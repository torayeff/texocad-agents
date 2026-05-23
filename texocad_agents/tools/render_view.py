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
    MAX_MODEL_BYTES,
    RENDER_CONTENT_TYPE,
    StoredBlob,
    escape_key,
    get_design_revision,
    get_design_tables,
    get_storage_container,
    revision_blob_name,
    save_design_preview,
    upload_blob,
)
from texocad_agents.tools._runtime import error_message, get_tool_context

RENDER_STYLES = {"ortho", "shaded", "both"}
RENDER_RUNNERS_DIR = Path(__file__).with_name("render_runners")
RENDER_CONFIG_FILE = "render_config.json"
SHARED_RUNNER_MODULE = "_shared.py"
ORTHO_STATUS_FILENAME = "_ortho.status"
SHADED_STATUS_FILENAME = "_shaded.status"
DEFAULT_RENDER_RUNNERS = (
    ("ortho", ORTHO_STATUS_FILENAME),
    ("shaded", SHADED_STATUS_FILENAME),
)


@tool(parse_docstring=True)
def render_view(
    revision_number: int,
    runtime: ToolRuntime,
    view: str = "iso",
    style: str = "both",
    camera: list[float] | None = None,
    up: list[float] | None = None,
) -> Command | list:
    """Render and store an additional view of a built CAD revision.

    Use this after create_cad when the default iso renders do not show an
    important feature clearly. It renders from the stored STEP export, not by
    re-running the CAD source.

    Args:
        revision_number: Stored design revision number to render.
        view: Camera view to render. Use one of "iso", "top", "bottom", "front",
            "back", "left", or "right".
        style: Render style. Use "both" (default) to produce ortho and shaded
            renders, or "ortho" / "shaded" for a single style.
        camera: Optional custom camera position vector (a viewpoint in world
            coordinates), for example [80, -120, 60]. Treated as the position
            looking at the origin; the view direction is `camera - target`.
        up: Optional custom up vector for camera renders, for example [0, 0, 1].
    """
    try:
        ctx = get_tool_context(runtime)
        if ctx is None:
            raise ValueError(
                "Refused: cannot read design artifacts without an authenticated owner and thread."
            )
        cam = _vector(camera)
        up_vec = _vector(up)
        resolved_style = _resolve_style(style)
        styles = ["ortho", "shaded"] if resolved_style == "both" else [resolved_style]

        tables = get_design_tables()
        try:
            revision = get_design_revision(
                tables.design_revisions,
                ctx.owner_id,
                ctx.design_id,
                int(revision_number),
            )
        except ResourceNotFoundError:
            raise ValueError(
                "Could not find design revision.\n"
                f"design_id: {ctx.design_id}\n"
                f"revision_number: {revision_number}"
            )

        export = _step_export(revision)
        container = get_storage_container()
        content_blocks: list = []
        view_names: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            blob_client = container.get_blob_client(export.blob_name)
            blob_size = blob_client.get_blob_properties().size
            if blob_size > MAX_MODEL_BYTES:
                raise ValueError(
                    f"Stored model {export.filename!r} is {blob_size} bytes, "
                    f"which exceeds the {MAX_MODEL_BYTES} byte render limit."
                )
            step_bytes = blob_client.download_blob().readall()
            save_render_kwargs = {
                "owner_id": ctx.owner_id,
                "design_id": ctx.design_id,
                "revision_number": int(revision_number),
                "source_export_checksum": export.checksum,
                "camera": cam,
                "up": up_vec,
                "thread_id": ctx.thread_id,
                "run_id": ctx.run_id,
            }

            for style_name in styles:
                base_view = "custom" if cam else view.lower()
                view_name = f"{base_view}_{style_name}"
                view_names.append(view_name)
                image_path = render_step(
                    step_bytes,
                    export.filename,
                    out_dir,
                    view=view,
                    style=style_name,
                    camera=cam,
                    up=up_vec,
                )

                render = save_render(
                    tables.design_previews,
                    container,
                    **save_render_kwargs,
                    image_path=image_path,
                    view=view_name,
                )

                content_blocks.append(create_text_block(text=f"{view_name} render"))
                content_blocks.append(
                    create_image_block(base64=render["image"], mime_type="image/png")
                )

        header = create_text_block(
            text=(
                f"Rendered and stored view(s): {', '.join(view_names)}.\n"
                f"design_id: {ctx.design_id}\n"
                f"revision_number: {revision_number}"
            )
        )
        return [header] + content_blocks
    except Exception as exc:
        return error_message(runtime, str(exc))


def save_render(
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
        raise ValueError(f"render PNG for view {view!r} was not produced")

    filename = f"{escape_key(view)}.png"
    blob_name = revision_blob_name(
        owner_id, design_id, revision_number, "previews", filename
    )
    render = upload_blob(
        storage_container,
        blob_name,
        image_path,
        filename=filename,
        content_type=RENDER_CONTENT_TYPE,
        content_disposition=f'inline; filename="{filename}"',
    )
    save_design_preview(
        design_previews_table,
        owner_id,
        design_id,
        revision_number,
        view,
        render,
        source_export_checksum=source_export_checksum,
        camera=camera,
        up=up,
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )
    image = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return {"blobs": [render], "image": image}


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
        _runner_source(style),
        out_dir,
        support_files={
            step_filename: step_bytes,
            RENDER_CONFIG_FILE: render_config(
                step_filename=step_filename,
                view=view,
                camera=camera,
                up=up,
            ),
            SHARED_RUNNER_MODULE: _runner_source("_shared").encode("utf-8"),
        },
    )
    if result.exit_code != 0:
        raise ValueError(
            "Render failed.\n"
            f"exit_code: {result.exit_code}\n"
            f"output:\n{result.output.strip()}"
        )

    renders = [path for path in result.files if path.suffix.lower() == ".png"]
    if len(renders) != 1:
        raise ValueError(f"Expected exactly one render PNG, found {len(renders)}.")
    return renders[0]


def build_render_command(view: str = "iso") -> SandboxCommand:
    runner_lines = "".join(
        f'echo "=== {style}.py ==="\n'
        f"python {style}.py 2>&1\n"
        f'echo "$?" > "$out/{status_filename}"\n'
        for style, status_filename in DEFAULT_RENDER_RUNNERS
    )
    script = f'set +e\nout="$TEXOCAD_OUTPUT_DIR"\n{runner_lines}exit 0\n'
    return SandboxCommand(
        filename="ortho.py",
        contents=_runner_source("ortho").encode("utf-8"),
        command=script,
        support_files={
            RENDER_CONFIG_FILE: render_config(
                step_filename="",
                view=view,
                camera=None,
                up=None,
            ),
            **{
                f"{style}.py": _runner_source(style).encode("utf-8")
                for style, _ in DEFAULT_RENDER_RUNNERS
                if style != "ortho"
            },
            SHARED_RUNNER_MODULE: _runner_source("_shared").encode("utf-8"),
        },
    )


def render_config(
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


def _step_export(revision: dict) -> StoredBlob:
    try:
        exports = json.loads(revision.get("exports_json") or "[]")
    except json.JSONDecodeError:
        raise ValueError("Stored revision has invalid exports metadata.")

    step_exports = [
        export
        for export in exports
        if str(export.get("filename", "")).lower().endswith(".step")
    ]
    if len(step_exports) != 1:
        raise ValueError(
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
    if value not in RENDER_STYLES:
        raise ValueError(
            f"Unknown render style {style!r}; expected: ortho, shaded, or both."
        )
    return value


def _runner_source(name: str) -> str:
    return (RENDER_RUNNERS_DIR / f"{name}.py").read_text()
