import base64
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

import fitz
from build123d import ColorIndex, ExportSVG, LineType, Unit, import_step
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from texocad_agents.runtime_files import read_runtime_file, support_files_for
from texocad_agents.sandbox import run_python

MODEL_EXTENSION = ".step"
MAX_MODEL_FILES = 1
MAX_MODEL_BYTES = 100 * 1024 * 1024
PREVIEW_IMAGE_SIZE = 1024
PREVIEW_VIEWS = {
    "iso": ((100, -100, 80), (0, 0, 1)),
    "front": ((0, -100, 0), (0, 0, 1)),
    "back": ((0, 100, 0), (0, 0, 1)),
    "left": ((-100, 0, 0), (0, 0, 1)),
    "right": ((100, 0, 0), (0, 0, 1)),
    "top": ((0, 0, 100), (0, 1, 0)),
    "bottom": ((0, 0, -100), (0, 1, 0)),
}


@dataclass(frozen=True)
class PreviewResult:
    exit_code: int
    output: str
    image_path: Path | None = None


@tool(parse_docstring=True)
def preview_design(
    file_path: str,
    runtime: ToolRuntime,
    view: str = "iso",
    camera: list[float] | None = None,
    up: list[float] | None = None,
) -> list | str:
    """Render a preview image for visual design verification.

    Use this before build_design to avoid creating bad design revisions.
    Compare the returned image with the user's request and any reference images
    or PDF pages that were read earlier.

    Args:
        file_path: Path to the CAD Python file in the agent filesystem.
        view: Camera view to render. Use one of "iso", "top", "bottom", "front",
            "back", "left", or "right".
        camera: Optional custom camera position vector, for example [80, -120, 60].
        up: Optional custom up vector for camera renders, for example [0, 0, 1].
    """
    code = read_runtime_file(file_path, runtime)
    if code is None:
        return f"Could not find {file_path!r} in the agent filesystem."

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_label = _preview_label(view, camera)
        result = _render_preview(
            code,
            Path(tmpdir),
            view=view,
            camera=_vector_tuple(camera),
            up=_vector_tuple(up),
            support_files=support_files_for(file_path, runtime),
        )
        if result.exit_code != 0 or result.image_path is None:
            return (
                "Visual verification failed.\n"
                f"exit_code: {result.exit_code}\n"
                f"output:\n{result.output.strip()}"
            )

        image_b64 = base64.b64encode(result.image_path.read_bytes()).decode("utf-8")

    return [
        create_text_block(text=f"Rendered deterministic {preview_label} preview."),
        create_image_block(base64=image_b64, mime_type="image/png"),
    ]


def _render_preview(
    code: str,
    out_dir: Path,
    *,
    view: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
    support_files: dict[str, bytes],
) -> PreviewResult:
    result = run_python(code, out_dir, support_files=support_files)
    if result.exit_code != 0:
        return PreviewResult(
            exit_code=result.exit_code,
            output=result.output,
        )

    step_file_or_error = _validated_step_file(result.files)
    if isinstance(step_file_or_error, str):
        return PreviewResult(exit_code=1, output=step_file_or_error)

    step_file = step_file_or_error
    try:
        image_path = _render_step_preview(
            step_file,
            out_dir,
            view=view,
            camera=camera,
            up=up,
        )
    except Exception as exc:
        return PreviewResult(
            exit_code=1,
            output=f"Preview render failed for {step_file.name!r}: {exc}",
        )

    return PreviewResult(
        exit_code=0,
        output=result.output,
        image_path=image_path,
    )


def _validated_step_file(files: tuple[Path, ...]) -> Path | str:
    unsupported_files = [
        file_path.name
        for file_path in files
        if file_path.suffix.lower() != MODEL_EXTENSION
    ]
    if unsupported_files:
        return (
            "Sandbox output must contain only .step files; found unsupported "
            f"file(s): {', '.join(unsupported_files)}."
        )

    step_files = tuple(
        file_path for file_path in files if file_path.suffix.lower() == MODEL_EXTENSION
    )
    if len(step_files) != MAX_MODEL_FILES:
        return f"Expected exactly one STEP export, found {len(step_files)}."

    step_file = step_files[0]
    model_size = step_file.stat().st_size
    if model_size > MAX_MODEL_BYTES:
        return (
            f"Sandbox produced oversized model file {step_file.name!r}: "
            f"{model_size} bytes; limit is {MAX_MODEL_BYTES} bytes."
        )

    return step_file


def _render_step_preview(
    step_file: Path,
    out_dir: Path,
    *,
    view: str = "iso",
    camera: tuple[float, float, float] | None = None,
    up: tuple[float, float, float] | None = None,
    size: int = PREVIEW_IMAGE_SIZE,
) -> Path:
    if camera is None:
        view_name = view.lower()
        if view_name not in PREVIEW_VIEWS:
            allowed = ", ".join(sorted(PREVIEW_VIEWS))
            raise ValueError(f"unknown view {view!r}; expected one of: {allowed}")
        viewport_origin, viewport_up = PREVIEW_VIEWS[view_name]
    else:
        view_name = "custom"
        viewport_origin = _validate_preview_vector(camera, "camera")
        viewport_up = _validate_preview_vector(up or (0, 0, 1), "up")
        _validate_preview_axes(viewport_origin, viewport_up)

    shape = import_step(step_file)
    visible, hidden = shape.project_to_viewport(
        viewport_origin,
        viewport_up,
        look_at=(0, 0, 0),
    )
    if not visible and not hidden:
        raise ValueError(f"{view_name} projection produced no visible or hidden edges")

    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{step_file.stem}_{view_name}.svg"
    png_path = out_dir / f"{step_file.stem}_{view_name}.png"

    exporter = ExportSVG(unit=Unit.MM, margin=10, fit_to_stroke=True)
    exporter.add_layer("Visible", line_color=ColorIndex.BLACK, line_weight=0.35)
    exporter.add_layer(
        "Hidden",
        line_color=ColorIndex.GRAY,
        line_weight=0.18,
        line_type=LineType.ISO_DOT,
    )
    if hidden:
        exporter.add_shape(hidden, layer="Hidden")
    if visible:
        exporter.add_shape(visible, layer="Visible")
    exporter.write(svg_path)

    _convert_svg_to_png(svg_path, png_path, size)
    return png_path


def _vector_tuple(vector: list[float] | None) -> tuple[float, float, float] | None:
    if vector is None:
        return None
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError("camera and up must be 3-number vectors when provided")
    return (float(vector[0]), float(vector[1]), float(vector[2]))


def _preview_label(view: str, camera: list[float] | None) -> str:
    if camera is not None:
        return "custom-camera"
    return view.lower()


def _validate_preview_vector(
    vector: tuple[float, float, float],
    name: str,
) -> tuple[float, float, float]:
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError(f"{name} must be a 3-number vector")

    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{name} must contain finite numbers")
    if _vector_length(values) == 0:
        raise ValueError(f"{name} must not be the zero vector")

    return values


def _validate_preview_axes(
    camera: tuple[float, float, float],
    up: tuple[float, float, float],
) -> None:
    cross = (
        camera[1] * up[2] - camera[2] * up[1],
        camera[2] * up[0] - camera[0] * up[2],
        camera[0] * up[1] - camera[1] * up[0],
    )
    if _vector_length(cross) <= 1e-9:
        raise ValueError("camera and up vectors must not be parallel")


def _vector_length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _convert_svg_to_png(svg_path: Path, png_path: Path, size: int) -> None:
    document = fitz.open(stream=svg_path.read_bytes(), filetype="svg")
    try:
        page = document[0]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        scale = size / max(width, height)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        png_path.write_bytes(pixmap.tobytes("png"))
    finally:
        document.close()
