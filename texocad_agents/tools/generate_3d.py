import base64
import io
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from PIL import Image, UnidentifiedImageError

from texocad_agents.sandbox.modal.client import generate_glb
from texocad_agents.storage import (
    GLB_CONTENT_TYPE,
    MAX_MODEL_BYTES,
    get_storage_container,
    revision_blob_name,
    save_design_preview,
    save_design_revision_blobs,
    upload_blob,
)
from texocad_agents.tools._runtime import (
    ToolContext,
    authorize_blob_access,
    create_revision,
    error_message,
    get_tool_context,
    revision_artifact,
)

PREVIEW_VIEW = "source_image"
INPUT_IMAGE_FORMATS: dict[str, tuple[str, str]] = {
    "PNG": (".png", "image/png"),
    "JPEG": (".jpg", "image/jpeg"),
    "WEBP": (".webp", "image/webp"),
}


@tool(parse_docstring=True)
def generate_3d(
    image: str,
    runtime: ToolRuntime,
    filename: str = "model.glb",
) -> Command:
    """Generate a 3D model (GLB) from an image and store it as a design revision.

    Use this only after the user has approved a preview image and explicitly
    asked for a first 3D draft. Generation runs on a GPU and can take several
    minutes. Pass the blob path or URL of the approved preview image, or a
    user-uploaded image attachment.

    Args:
        image: Blob path or URL of the source image to convert. Use the active
            preview's `sketch_image_blob` value or an uploaded attachment path.
        runtime: LangGraph tool runtime for the current tool call.
        filename: Informative GLB filename for the stored 3D artifact. Defaults
            to "model.glb".

    Returns:
        LangGraph command containing either an error message or a stored 3D
        model revision artifact.
    """

    glb_filename, filename_error = _normalize_glb_filename(filename)
    if filename_error:
        return error_message(runtime, filename_error)

    ctx = get_tool_context(runtime)
    if ctx is None:
        return error_message(
            runtime,
            "Refused: cannot create 3D artifacts without an authenticated owner and thread.",
        )

    storage_container = get_storage_container()
    image_bytes, download_error = _download_source_image(storage_container, image, ctx)
    if download_error:
        return error_message(runtime, download_error)

    source_format = _validate_image_format(image_bytes)
    if source_format is None:
        return error_message(
            runtime,
            "Refused source image: unsupported or unreadable image; "
            "supported formats are PNG, JPEG, and WEBP.",
        )
    source_ext, source_content_type = source_format
    source_filename = f"{Path(glb_filename).stem}{source_ext}"

    try:
        glb_bytes = generate_glb(image_bytes, source_filename)
    except Exception as exc:
        return error_message(runtime, f"3D generation failed: {exc}")

    if not glb_bytes:
        return error_message(runtime, "3D generation returned no model data.")
    if len(glb_bytes) > MAX_MODEL_BYTES:
        return error_message(
            runtime,
            f"Generated model is too large: {len(glb_bytes)} bytes; "
            f"limit is {MAX_MODEL_BYTES} bytes.",
        )

    try:
        return _store_model(
            runtime,
            ctx=ctx,
            storage_container=storage_container,
            image_bytes=image_bytes,
            source_filename=source_filename,
            source_content_type=source_content_type,
            glb_filename=glb_filename,
            glb_bytes=glb_bytes,
        )
    except Exception as exc:
        return error_message(runtime, f"Failed to persist 3D model revision: {exc}")


def _store_model(
    runtime: ToolRuntime,
    *,
    ctx: ToolContext,
    storage_container,
    image_bytes: bytes,
    source_filename: str,
    source_content_type: str,
    glb_filename: str,
    glb_bytes: bytes,
) -> Command:
    """Persist the source image and generated GLB and build the tool response.

    Args:
        runtime: LangGraph tool runtime for the current tool call.
        ctx: Authenticated tool context.
        storage_container: Azure Blob container client.
        image_bytes: Source image bytes fed to the 3D generator.
        source_filename: Filename for the stored source image artifact.
        source_content_type: MIME type of the source image.
        glb_filename: User-facing GLB filename for the stored 3D artifact.
        glb_bytes: Generated GLB file contents.

    Returns:
        LangGraph command with a tool message and 3D model artifact metadata.
    """

    tables, _, design_revision, revision_number = create_revision(
        ctx, title=ctx.metadata.get("title") or "3D model"
    )

    source_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "source", source_filename
        ),
        image_bytes,
        filename=source_filename,
        content_type=source_content_type,
        content_disposition=f'inline; filename="{source_filename}"',
    )
    export_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "exports", glb_filename
        ),
        glb_bytes,
        filename=glb_filename,
        content_type=GLB_CONTENT_TYPE,
        content_disposition=f'attachment; filename="{glb_filename}"',
    )
    render_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "previews", source_filename
        ),
        image_bytes,
        filename=source_filename,
        content_type=source_content_type,
        content_disposition=f'inline; filename="{source_filename}"',
    )
    save_design_preview(
        tables.design_previews,
        ctx.owner_id,
        ctx.design_id,
        revision_number,
        PREVIEW_VIEW,
        render_blob,
        source_export_checksum=export_blob.checksum,
        camera=None,
        up=None,
        created_by_thread_id=ctx.thread_id,
        created_by_run_id=ctx.run_id,
    )

    design_revision = save_design_revision_blobs(
        tables.design_revisions, design_revision, source_blob, [export_blob]
    )

    content_blocks = [
        create_text_block(
            text=(
                "Created 3D model revision.\n"
                f"design_id: {ctx.design_id}\n"
                f"revision_number: {revision_number}\n"
                f"model_glb_blob: {export_blob.blob_name}\n"
                f"size_bytes: {len(glb_bytes)}"
            )
        ),
        create_image_block(
            base64=base64.b64encode(image_bytes).decode("utf-8"),
            mime_type=source_content_type,
        ),
    ]

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content_blocks,
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": revision_artifact(
                            artifact_type="texocad.model_revision",
                            design_id=ctx.design_id,
                            design_revision=design_revision,
                            source_blob=source_blob,
                            exports=[export_blob],
                            renders=[render_blob],
                            thread_id=ctx.thread_id,
                            run_id=ctx.run_id,
                        )
                    },
                )
            ]
        }
    )


def _normalize_glb_filename(filename: str) -> tuple[str | None, str | None]:
    """Validate the explicit GLB artifact filename.

    Args:
        filename: Raw filename provided to the tool.

    Returns:
        Tuple of normalized filename and optional error message.
    """

    normalized = filename.strip()
    if not normalized:
        return None, "Invalid filename: filename must not be empty."
    if "/" in normalized or "\\" in normalized:
        return None, "Invalid filename: filename must not include path separators."
    if any(char in normalized for char in {'"', "\r", "\n"}):
        return None, "Invalid filename: filename must not include quotes or newlines."
    if not normalized.lower().endswith(".glb"):
        normalized = f"{normalized}.glb"
    return normalized, None


def _validate_image_format(image_bytes: bytes) -> tuple[str, str] | None:
    """Validate source image bytes and resolve extension and content type.

    Args:
        image_bytes: Raw source image bytes.

    Returns:
        Tuple of file extension and MIME type for supported images, otherwise
        None.
    """

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return INPUT_IMAGE_FORMATS.get(image_format)


def _download_source_image(
    storage_container,
    image_path: str,
    ctx: ToolContext,
) -> tuple[bytes | None, str | None]:
    """Authorize and download the source image blob.

    Args:
        storage_container: Azure Blob container client used to fetch the image.
        image_path: Blob path or URL of the source image.
        ctx: Authenticated tool context for the current caller.

    Returns:
        Tuple of downloaded bytes and optional error message.
    """

    blob_name = authorize_blob_access(image_path, ctx)
    if blob_name is None:
        return (
            None,
            f"Refused source image {image_path!r}: not in this thread/user scope.",
        )
    try:
        return (
            storage_container.get_blob_client(blob_name).download_blob().readall(),
            None,
        )
    except Exception as exc:
        return None, f"Could not download source image from {image_path!r}: {exc}"
