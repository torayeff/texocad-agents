"""Render additional stored views for existing CAD revisions."""

import json
import tempfile
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.storage import (
    MAX_MODEL_BYTES,
    StoredBlob,
    get_design_revision,
    get_design_tables,
    get_storage_container,
)
from texocad_agents.tools._rendering import render_step, save_render
from texocad_agents.tools._runtime import error_message, get_tool_context

RENDER_STYLES = {"ortho", "shaded", "both"}


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
        runtime: LangGraph tool runtime for the current tool call.
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


def _step_export(revision: dict) -> StoredBlob:
    """Extract the single STEP export recorded on a revision.

    Args:
        revision: Stored design revision entity.

    Returns:
        Stored blob metadata for the STEP export.

    Raises:
        ValueError: If export metadata is invalid or does not contain exactly
            one STEP file.
    """

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
    """Normalize an optional three-number vector to floats.

    Args:
        vector: Optional list or tuple containing three numeric values.

    Returns:
        Three-float tuple, or None when no vector is provided.

    Raises:
        ValueError: If `vector` is not a three-item sequence.
    """

    if vector is None:
        return None
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError("camera and up must be 3-number vectors when provided")
    return (float(vector[0]), float(vector[1]), float(vector[2]))


def _resolve_style(style: str) -> str:
    """Validate and normalize a render style name.

    Args:
        style: Requested render style.

    Returns:
        Lowercase render style.

    Raises:
        ValueError: If `style` is not supported.
    """

    value = style.lower()
    if value not in RENDER_STYLES:
        raise ValueError(
            f"Unknown render style {style!r}; expected: ortho, shaded, or both."
        )
    return value
