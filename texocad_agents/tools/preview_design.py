import base64
import json
import tempfile
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from texocad_agents.sandbox import SandboxCommand, run_python
from texocad_agents.storage import (
    StoredBlob,
    get_design_revision,
    get_design_tables,
    get_storage_container,
    save_design_preview,
    upload_preview_blob,
)

MODEL_EXTENSION = ".step"
MAX_MODEL_FILES = 1
VIEWS = {
    "iso": ((100, -100, 80), (0, 0, 1)),
    "front": ((0, -100, 0), (0, 0, 1)),
    "back": ((0, 100, 0), (0, 0, 1)),
    "left": ((-100, 0, 0), (0, 0, 1)),
    "right": ((100, 0, 0), (0, 0, 1)),
    "top": ((0, 0, 100), (0, 1, 0)),
    "bottom": ((0, 0, -100), (0, 1, 0)),
}


@tool(parse_docstring=True)
def preview_design(
    revision_number: int,
    runtime: ToolRuntime,
    view: str = "iso",
    camera: list[float] | None = None,
    up: list[float] | None = None,
) -> list | str:
    """Render and store an additional preview for a built design revision.

    Use this after build_design when the default iso preview does not show an
    important feature clearly. It renders from the stored STEP export, not by
    re-running the CAD source.

    Args:
        revision_number: Stored design revision number to preview.
        view: Camera view to render. Use one of "iso", "top", "bottom", "front",
            "back", "left", or "right".
        camera: Optional custom camera position vector, for example [80, -120, 60].
        up: Optional custom up vector for camera renders, for example [0, 0, 1].
    """
    ctx = _context(runtime)
    if isinstance(ctx, str):
        return ctx
    owner_id, design_id, thread_id, run_id = ctx

    cam = _vector(camera)
    up_vec = _vector(up)
    view_name = _view_name(view, cam)

    tables = get_design_tables()
    try:
        revision = get_design_revision(
            tables.design_revisions,
            owner_id,
            design_id,
            int(revision_number),
        )
    except ResourceNotFoundError:
        return (
            "Could not find design revision.\n"
            f"design_id: {design_id}\n"
            f"revision_number: {revision_number}"
        )

    export = _step_export(revision)
    if isinstance(export, str):
        return export

    container = get_storage_container()
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        blob = container.get_blob_client(export.blob_name)
        image_path_or_error = render_step(
            blob.download_blob().readall(),
            export.filename,
            out_dir,
            view=view,
            camera=cam,
            up=up_vec,
        )
        if isinstance(image_path_or_error, str):
            return image_path_or_error
        image_path = image_path_or_error

        preview = save_preview(
            tables.design_previews,
            container,
            owner_id=owner_id,
            design_id=design_id,
            revision_number=int(revision_number),
            image_path=image_path,
            view=view_name,
            source_export_checksum=export.checksum,
            camera=cam,
            up=up_vec,
            thread_id=thread_id,
            run_id=run_id,
        )

    return [
        create_text_block(
            text=(
                f"Rendered and stored {view_name} preview.\n"
                f"design_id: {design_id}\n"
                f"revision_number: {revision_number}\n"
                f"preview_blob: {preview['blobs'][0].blob_name}"
            )
        ),
        create_image_block(base64=preview["image"], mime_type="image/png"),
    ]


def save_preview(
    design_previews_table,
    storage_container,
    *,
    owner_id: str,
    design_id: str,
    revision_number: int,
    image_path: Path | None,
    view: str,
    source_export_checksum: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
    thread_id: str,
    run_id: str | None,
) -> dict:
    if image_path is None:
        return {"blobs": [], "image": None, "error": "preview PNG was not produced"}

    try:
        preview = upload_preview_blob(
            storage_container,
            owner_id,
            design_id,
            revision_number,
            image_path,
            view,
        )
        save_design_preview(
            design_previews_table,
            owner_id,
            design_id,
            revision_number,
            view,
            preview,
            source_export_checksum=source_export_checksum,
            camera=camera,
            up=up,
            created_by_thread_id=thread_id,
            created_by_run_id=run_id,
        )
        image = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        return {"blobs": [preview], "image": image, "error": None}
    except Exception as exc:
        return {"blobs": [], "image": None, "error": str(exc)}


def preview_status(preview: dict) -> str:
    if preview["blobs"]:
        return f"\npreview_blob: {preview['blobs'][0].blob_name}"
    if preview["error"]:
        return f"\npreview_failed: {preview['error']}"
    return ""


def render_step(
    step_bytes: bytes,
    step_filename: str,
    out_dir: Path,
    *,
    view: str = "iso",
    camera: tuple[float, float, float] | None = None,
    up: tuple[float, float, float] | None = None,
) -> Path | str:
    result = run_python(
        _preview_script(
            step_filename,
            view=view,
            camera=camera,
            up=up,
        ),
        out_dir,
        support_files={step_filename: step_bytes},
    )
    if result.exit_code != 0:
        return (
            "Preview render failed.\n"
            f"exit_code: {result.exit_code}\n"
            f"output:\n{result.output.strip()}"
        )

    previews = [path for path in result.files if path.suffix.lower() == ".png"]
    if len(previews) != 1:
        return f"Expected exactly one preview PNG, found {len(previews)}."
    return previews[0]


def preview_command(view: str = "iso") -> SandboxCommand:
    return SandboxCommand(
        filename="render_preview.py",
        contents=_preview_script(
            "",
            view=view,
            camera=None,
            up=None,
        ).encode("utf-8"),
        command="python render_preview.py 2>&1 || true",
    )


def _context(runtime: ToolRuntime) -> tuple[str, str, str, str | None] | str:
    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity")
    if not owner_id:
        return "Refused: cannot read design artifacts without an authenticated owner."

    raw_thread_id = configurable.get("thread_id")
    if not raw_thread_id:
        return "Refused: cannot read design artifacts without a LangGraph thread_id."

    thread_id = str(raw_thread_id)
    run_id = configurable.get("run_id") or metadata.get("run_id")
    return owner_id, thread_id, thread_id, run_id


def _step_export(revision: dict) -> StoredBlob | str:
    try:
        exports = json.loads(revision.get("exports_json") or "[]")
    except json.JSONDecodeError:
        return "Stored revision has invalid exports metadata."

    step_exports = [
        export
        for export in exports
        if str(export.get("filename", "")).lower().endswith(MODEL_EXTENSION)
    ]
    if len(step_exports) != MAX_MODEL_FILES:
        return (
            f"Expected exactly one STEP export on stored revision, "
            f"found {len(step_exports)}."
        )

    export = step_exports[0]
    return StoredBlob(
        filename=str(export["filename"]),
        blob_name=str(export["blob_name"]),
        content_type=str(export["content_type"]),
        checksum=str(export["checksum"]),
    )


def _vector(vector: list[float] | None) -> tuple[float, float, float] | None:
    if vector is None:
        return None
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError("camera and up must be 3-number vectors when provided")
    return (float(vector[0]), float(vector[1]), float(vector[2]))


def _view_name(view: str, camera: tuple[float, float, float] | None) -> str:
    return "custom" if camera is not None else view.lower()


def _preview_script(
    step_filename: str,
    *,
    view: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
) -> str:
    views_repr = repr(VIEWS)
    camera_repr = repr(camera)
    up_repr = repr(up)
    return f"""
import math
import os
from pathlib import Path

import fitz
from build123d import ColorIndex, ExportSVG, LineType, Unit, import_step

VIEWS = {views_repr}
STEP_FILE = {step_filename!r}
VIEW = {view!r}
CAMERA = {camera_repr}
UP = {up_repr}
SIZE = 1024


def main():
    out_dir = Path(os.environ["TEXOCAD_OUTPUT_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    step_path = find_step(out_dir)
    image_path = render_step(step_path, out_dir)
    print(f"preview: {{image_path.name}}")


def find_step(out_dir):
    if STEP_FILE:
        step_path = out_dir / STEP_FILE
        if step_path.exists():
            return step_path
        return Path(STEP_FILE)

    step_files = list(out_dir.glob("*.step"))
    if len(step_files) != 1:
        raise ValueError(f"expected exactly one STEP file, found {{len(step_files)}}")
    return step_files[0]


def render_step(step_file, out_dir):
    if CAMERA is None:
        view_name = VIEW.lower()
        if view_name not in VIEWS:
            allowed = ", ".join(sorted(VIEWS))
            raise ValueError(f"unknown view {{VIEW!r}}; expected one of: {{allowed}}")
        origin, viewport_up = VIEWS[view_name]
    else:
        view_name = "custom"
        origin = check_vector(CAMERA, "camera")
        viewport_up = check_vector(UP or (0, 0, 1), "up")
        check_axes(origin, viewport_up)

    shape = import_step(step_file)
    visible, hidden = shape.project_to_viewport(
        origin,
        viewport_up,
        look_at=(0, 0, 0),
    )
    if not visible and not hidden:
        raise ValueError(f"{{view_name}} projection produced no visible or hidden edges")

    svg_path = Path(f"{{step_file.stem}}_{{view_name}}.svg")
    png_path = out_dir / f"{{step_file.stem}}_{{view_name}}.png"

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

    svg_to_png(svg_path, png_path)
    return png_path


def check_vector(vector, name):
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError(f"{{name}} must be a 3-number vector")

    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{{name}} must contain finite numbers")
    if vector_length(values) == 0:
        raise ValueError(f"{{name}} must not be the zero vector")

    return values


def check_axes(camera, up):
    cross = (
        camera[1] * up[2] - camera[2] * up[1],
        camera[2] * up[0] - camera[0] * up[2],
        camera[0] * up[1] - camera[1] * up[0],
    )
    if vector_length(cross) <= 1e-9:
        raise ValueError("camera and up vectors must not be parallel")


def vector_length(vector):
    return math.sqrt(sum(value * value for value in vector))


def svg_to_png(svg_path, png_path):
    document = fitz.open(stream=svg_path.read_bytes(), filetype="svg")
    try:
        page = document[0]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        scale = SIZE / max(width, height)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        png_path.write_bytes(pixmap.tobytes("png"))
    finally:
        document.close()


main()
""".strip()
