import base64
import tempfile
from pathlib import Path

from daytona import Daytona
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from texocad_agents.runtime_files import read_runtime_file, support_files_for
from texocad_agents.sandbox import SANDBOX_CONFIG, ensure_snapshot, run_code_preview


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

    daytona = Daytona()
    ensure_snapshot(daytona, SANDBOX_CONFIG)
    with tempfile.TemporaryDirectory() as tmpdir:
        preview_label = _preview_label(view, camera)
        result = run_code_preview(
            daytona,
            code,
            Path(tmpdir),
            SANDBOX_CONFIG,
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
