"""Generate or edit sketch images and persist them as design revisions."""

import base64
import io
import os

from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from openai import OpenAI
from PIL import Image, UnidentifiedImageError

from texocad_agents.storage import (
    RENDER_CONTENT_TYPE,
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

SKETCH_CONTENT_TYPE = "image/png"
PROMPT_CONTENT_TYPE = "text/plain; charset=utf-8"
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_SIZES = {
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "auto",
}
MAX_INPUT_IMAGES = 4


@tool(parse_docstring=True)
def create_sketch(
    prompt: str,
    runtime: ToolRuntime,
    quality: str = "auto",
    size: str = "auto",
    images: list[str] | None = None,
    mask: str | None = None,
) -> Command:
    """Generate or edit an image and store it as a design revision.

    When images is empty or omitted, generates a new image from the prompt.
    When images is provided, edits or composes using those images as references.
    Images can be user-uploaded attachment paths, previous revision export paths,
    or any accessible blob path.

    Args:
        prompt: Detailed text description of the image to generate, or edit instructions when images are provided.
        runtime: LangGraph tool runtime for the current tool call.
        quality: Image quality - "low", "medium", "high", or "auto".
        size: Image dimensions - "1024x1024", "1536x1024", "1024x1536", "2048x2048", or "auto".
        images: List of blob paths or URLs for input images. Can include user-uploaded attachments or previous revision exports. When provided, the model edits or uses them as reference.
        mask: Blob path or URL of a mask image (PNG with alpha channel). Transparent areas indicate where the model should edit. Applied to the first image. Only used when images is provided.
    """
    validation_error = _validate_args(quality, size, images, mask)
    if validation_error:
        return error_message(runtime, validation_error)

    ctx = get_tool_context(runtime)
    if ctx is None:
        return error_message(
            runtime,
            "Refused: cannot create sketch artifacts without an authenticated owner and thread.",
        )

    storage_container = get_storage_container()
    image_buffers, mask_buffer, fetch_error = _fetch_input_buffers(
        storage_container, ctx, images, mask
    )
    if fetch_error:
        return error_message(runtime, fetch_error)

    openai_base_url = os.environ.get("OPENAI_API_BASE")
    client = OpenAI(base_url=openai_base_url) if openai_base_url else OpenAI()
    try:
        if image_buffers:
            edit_kwargs: dict = {
                "model": "gpt-image-2",
                "image": image_buffers if len(image_buffers) > 1 else image_buffers[0],
                "prompt": prompt,
                "quality": quality,
                "size": size,
            }
            if mask_buffer:
                edit_kwargs["mask"] = mask_buffer
            result = client.images.edit(**edit_kwargs)
        else:
            result = client.images.generate(
                model="gpt-image-2",
                prompt=prompt,
                quality=quality,
                size=size,
            )
    except Exception as exc:
        return error_message(runtime, f"Image generation failed: {exc}")

    image_base64 = result.data[0].b64_json
    if not image_base64:
        return error_message(runtime, "Image generation returned no image data.")
    image_bytes = base64.b64decode(image_base64)

    if image_error := _validate_image_bytes(image_bytes):
        return error_message(
            runtime, f"Generated image is not a valid PNG: {image_error}"
        )

    try:
        return _store_sketch(
            runtime,
            ctx=ctx,
            prompt=prompt,
            image_bytes=image_bytes,
            image_base64=image_base64,
        )
    except Exception as exc:
        return error_message(runtime, f"Failed to persist sketch revision: {exc}")


def _store_sketch(
    runtime: ToolRuntime,
    *,
    ctx: ToolContext,
    prompt: str,
    image_bytes: bytes,
    image_base64: str,
) -> Command:
    """Persist generated sketch artifacts and build the tool response.

    Args:
        runtime: LangGraph tool runtime for the current tool call.
        ctx: Authenticated tool context.
        prompt: Prompt text used to generate or edit the image.
        image_bytes: PNG image bytes returned by the image model.
        image_base64: Base64-encoded PNG image data for the response.

    Returns:
        LangGraph command with a tool message and sketch artifact metadata.
    """

    tables, storage_container, design_revision, revision_number = create_revision(
        ctx, title=ctx.metadata.get("title") or "Sketch"
    )

    source_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "source", "prompt.txt"
        ),
        prompt.encode("utf-8"),
        filename="prompt.txt",
        content_type=PROMPT_CONTENT_TYPE,
    )
    export_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "exports", "sketch.png"
        ),
        image_bytes,
        filename="sketch.png",
        content_type=SKETCH_CONTENT_TYPE,
        content_disposition='inline; filename="sketch.png"',
    )
    render_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "previews", "sketch.png"
        ),
        image_bytes,
        filename="sketch.png",
        content_type=RENDER_CONTENT_TYPE,
        content_disposition='inline; filename="sketch.png"',
    )
    save_design_preview(
        tables.design_previews,
        ctx.owner_id,
        ctx.design_id,
        revision_number,
        "sketch",
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
                "Created sketch revision.\n"
                f"design_id: {ctx.design_id}\n"
                f"revision_number: {revision_number}"
            )
        ),
        create_text_block(text="sketch render"),
        create_image_block(base64=image_base64, mime_type="image/png"),
    ]

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content_blocks,
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": revision_artifact(
                            artifact_type="texocad.sketch_revision",
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


def _validate_args(
    quality: str,
    size: str,
    images: list[str] | None,
    mask: str | None,
) -> str | None:
    """Validate image generation options.

    Args:
        quality: Requested image quality.
        size: Requested image size.
        images: Optional input image blob paths.
        mask: Optional mask image blob path.

    Returns:
        Error message when validation fails, otherwise None.
    """

    if quality not in ALLOWED_QUALITIES:
        return (
            f"Invalid quality {quality!r}. "
            f"Must be one of: {', '.join(sorted(ALLOWED_QUALITIES))}."
        )
    if size not in ALLOWED_SIZES:
        return (
            f"Invalid size {size!r}. "
            f"Must be one of: {', '.join(sorted(ALLOWED_SIZES))}."
        )
    if images and len(images) > MAX_INPUT_IMAGES:
        return f"Too many input images ({len(images)}). Maximum is {MAX_INPUT_IMAGES}."
    if mask and not images:
        return "A mask can only be used together with images."
    return None


def _fetch_input_buffers(
    storage_container,
    ctx: ToolContext,
    images: list[str] | None,
    mask: str | None,
) -> tuple[list[io.BytesIO], io.BytesIO | None, str | None]:
    """Download and validate input image and mask buffers.

    Args:
        storage_container: Azure Blob container client.
        ctx: Authenticated tool context.
        images: Optional input image blob paths.
        mask: Optional mask image blob path.

    Returns:
        Tuple of `(image_buffers, mask_buffer, error)`.
    """

    image_buffers: list[io.BytesIO] = []
    for image_path in images or []:
        buffer, error = _download_validated_image(storage_container, image_path, ctx)
        if buffer is None:
            return [], None, error
        image_buffers.append(buffer)

    mask_buffer: io.BytesIO | None = None
    if mask:
        mask_buffer, error = _download_validated_image(storage_container, mask, ctx)
        if mask_buffer is None:
            return [], None, error

    return image_buffers, mask_buffer, None


def _download_validated_image(
    storage_container,
    image_path: str,
    ctx: ToolContext,
) -> tuple[io.BytesIO | None, str | None]:
    """Download a user-scoped image and verify it can be opened.

    Args:
        storage_container: Azure Blob container client.
        image_path: Blob path or URL to authorize and download.
        ctx: Authenticated tool context.

    Returns:
        Tuple of `(image_buffer, error)`.
    """

    blob_name = authorize_blob_access(image_path, ctx)
    if blob_name is None:
        return (
            None,
            f"Refused image {image_path!r}: not in this thread/user scope.",
        )

    try:
        image_bytes = (
            storage_container.get_blob_client(blob_name).download_blob().readall()
        )
    except Exception as exc:
        return None, f"Could not download image from {image_path!r}: {exc}"

    if image_error := _validate_image_bytes(image_bytes):
        return (
            None,
            f"Refused image {image_path!r}: not a readable image ({image_error}).",
        )

    return io.BytesIO(image_bytes), None


def _validate_image_bytes(image_bytes: bytes) -> str | None:
    """Verify image bytes with Pillow.

    Args:
        image_bytes: Raw image bytes.

    Returns:
        Error text when validation fails, otherwise None.
    """

    try:
        Image.open(io.BytesIO(image_bytes)).verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return str(exc)
    return None
