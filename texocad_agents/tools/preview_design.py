import base64
import json
import tempfile
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.sandbox import SandboxCommand, run_python
from texocad_agents.storage import (
    StoredBlob,
    get_design_revision,
    get_design_tables,
    get_storage_container,
    save_design_preview,
    upload_preview_blob,
)
from texocad_agents.tools.tool_runtime import error_tool_message

PREVIEW_STYLES = {"ortho", "render", "both"}
PREVIEW_SCRIPTS_DIR = Path(__file__).with_name("preview_scripts")
PREVIEW_CONFIG_FILE = "preview_config.json"


class PreviewError(Exception):
    pass


@tool(parse_docstring=True)
def preview_design(
    revision_number: int,
    runtime: ToolRuntime,
    view: str = "iso",
    style: str = "both",
    camera: list[float] | None = None,
    up: list[float] | None = None,
) -> Command | list:
    """Render and store an additional preview for a built design revision.

    Use this after build_design when the default iso preview does not show an
    important feature clearly. It renders from the stored STEP export, not by
    re-running the CAD source.

    Args:
        revision_number: Stored design revision number to preview.
        view: Camera view to render. Use one of "iso", "top", "bottom", "front",
            "back", "left", or "right".
        style: Preview style. Use "both" (default) to produce ortho and render
            previews, or "ortho" / "render" for a single style.
        camera: Optional custom camera position vector, for example [80, -120, 60].
        up: Optional custom up vector for camera renders, for example [0, 0, 1].
    """
    try:
        owner_id, design_id, thread_id, run_id = _context(runtime)
        cam = _vector(camera)
        up_vec = _vector(up)
        preview_style = _resolve_style(style)
        styles = ["ortho", "render"] if preview_style == "both" else [preview_style]

        tables = get_design_tables()
        try:
            revision = get_design_revision(
                tables.design_revisions,
                owner_id,
                design_id,
                int(revision_number),
            )
        except ResourceNotFoundError:
            raise PreviewError(
                "Could not find design revision.\n"
                f"design_id: {design_id}\n"
                f"revision_number: {revision_number}"
            )

        export = _step_export(revision)
        container = get_storage_container()
        content_blocks: list = []
        view_names: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            blob = container.get_blob_client(export.blob_name)
            step_bytes = blob.download_blob().readall()

            for render_style in styles:
                view_name = f"{'custom' if cam else view.lower()}_{render_style}"
                view_names.append(view_name)
                image_path = render_step(
                    step_bytes,
                    export.filename,
                    out_dir,
                    view=view,
                    style=render_style,
                    camera=cam,
                    up=up_vec,
                )

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

                content_blocks.append(create_text_block(text=f"{view_name} preview"))
                content_blocks.append(
                    create_image_block(base64=preview["image"], mime_type="image/png")
                )

        header = create_text_block(
            text=(
                f"Rendered and stored preview(s): {', '.join(view_names)}.\n"
                f"design_id: {design_id}\n"
                f"revision_number: {revision_number}"
            )
        )
        return [header] + content_blocks
    except (PreviewError, ValueError) as exc:
        return error_tool_message(runtime, str(exc))


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
        raise PreviewError("preview PNG was not produced")

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
    return {"blobs": [preview], "image": image}


def render_step(
    step_bytes: bytes,
    step_filename: str,
    out_dir: Path,
    *,
    view: str = "iso",
    style: str = "ortho",
    camera: tuple[float, float, float] | None = None,
    up: tuple[float, float, float] | None = None,
) -> Path:
    result = run_python(
        _runner_script(style),
        out_dir,
        support_files={
            step_filename: step_bytes,
            PREVIEW_CONFIG_FILE: preview_config(
                step_filename=step_filename,
                view=view,
                camera=camera,
                up=up,
            ),
        },
    )
    if result.exit_code != 0:
        raise PreviewError(
            "Preview render failed.\n"
            f"exit_code: {result.exit_code}\n"
            f"output:\n{result.output.strip()}"
        )

    previews = [path for path in result.files if path.suffix.lower() == ".png"]
    if len(previews) != 1:
        raise PreviewError(f"Expected exactly one preview PNG, found {len(previews)}.")
    return previews[0]


def build_preview_command(view: str = "iso") -> SandboxCommand:
    return SandboxCommand(
        filename="ortho.py",
        contents=_runner_script("ortho").encode("utf-8"),
        command="python ortho.py 2>&1; python render.py 2>&1; true",
        support_files={
            PREVIEW_CONFIG_FILE: preview_config(
                step_filename="",
                view=view,
                camera=None,
                up=None,
            ),
            "render.py": _runner_script("render").encode("utf-8"),
        },
    )


def preview_config(
    *,
    step_filename: str,
    view: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
) -> bytes:
    return json.dumps({
        "step_filename": step_filename,
        "view": view,
        "camera": camera,
        "up": up,
    }).encode("utf-8")


def _context(runtime: ToolRuntime) -> tuple[str, str, str, str | None]:
    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity")
    if not owner_id:
        raise PreviewError(
            "Refused: cannot read design artifacts without an authenticated owner."
        )

    raw_thread_id = configurable.get("thread_id")
    if not raw_thread_id:
        raise PreviewError(
            "Refused: cannot read design artifacts without a LangGraph thread_id."
        )

    thread_id = str(raw_thread_id)
    run_id = configurable.get("run_id") or metadata.get("run_id")
    return owner_id, thread_id, thread_id, run_id


def _step_export(revision: dict) -> StoredBlob:
    try:
        exports = json.loads(revision.get("exports_json") or "[]")
    except json.JSONDecodeError:
        raise PreviewError("Stored revision has invalid exports metadata.")

    step_exports = [
        export
        for export in exports
        if str(export.get("filename", "")).lower().endswith(".step")
    ]
    if len(step_exports) != 1:
        raise PreviewError(
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


def _resolve_style(style: str) -> str:
    value = style.lower()
    if value not in PREVIEW_STYLES:
        raise PreviewError(
            f"Unknown preview style {style!r}; expected: ortho, render, or both."
        )
    return value


def _runner_script(style: str) -> str:
    return (PREVIEW_SCRIPTS_DIR / f"{style}.py").read_text()
