"""Generate or edit sketch images and persist them as design revisions."""

import base64
import io
import json
import os

from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from openai import OpenAI
from PIL import Image, UnidentifiedImageError

from texocad_agents.sketch_agent.prompt import IMAGE_PROMPT_TEMPLATE
from texocad_agents.storage import (
    RENDER_CONTENT_TYPE,
    get_design_tables,
    get_storage_container,
    list_design_revisions,
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
DEFAULT_QUALITY = "low"
DEFAULT_SIZE = "1024x1024"
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
MAX_INPUT_IMAGES = 4
SUPPORTED_INPUT_IMAGE_FORMATS = {
    "JPEG": ("image.jpg", "image/jpeg"),
    "PNG": ("image.png", "image/png"),
    "WEBP": ("image.webp", "image/webp"),
}
ImageUpload = tuple[str, io.BytesIO, str]


@tool(parse_docstring=True)
def create_sketch(
    prompt: str,
    runtime: ToolRuntime,
    quality: str = DEFAULT_QUALITY,
    images: list[str] | None = None,
    mask: str | None = None,
    edit_previous: bool = False,
) -> Command:
    """Generate or edit an image and store it as a design revision.

    When images is empty or omitted, generates a new image from the prompt.
    When images is provided, edits or composes using those images as references.
    Images can be user-uploaded attachment paths, previous revision export paths,
    or any accessible blob path.

    Args:
        prompt: Detailed text description of the image to generate, or edit instructions when images are provided.
        runtime: LangGraph tool runtime for the current tool call.
        quality: Image quality - "low" for drafts, "medium", "high" for final output, or "auto". Defaults to "low".
        images: List of blob paths or URLs for input images. Can include user-uploaded attachments or previous revision exports. When provided, the model edits or uses them as reference.
        mask: Blob path or URL of a mask image (PNG with alpha channel). Transparent areas indicate where the model should edit. Applied to the first image. Only used when images is provided.
        edit_previous: Set true when the user is editing the current sketch. If images is omitted, the latest sketch export from this thread is used as the input image.
    """
    if quality not in ALLOWED_QUALITIES:
        return error_message(
            runtime,
            f"Invalid quality {quality!r}. "
            f"Must be one of: {', '.join(sorted(ALLOWED_QUALITIES))}.",
        )
    if images and len(images) > MAX_INPUT_IMAGES:
        return error_message(
            runtime,
            f"Too many input images ({len(images)}). Maximum is {MAX_INPUT_IMAGES}.",
        )
    if mask and not (images or edit_previous):
        return error_message(runtime, "A mask can only be used together with images.")

    ctx = get_tool_context(runtime)
    if ctx is None:
        return error_message(
            runtime,
            "Refused: cannot create sketch artifacts without an authenticated owner and thread.",
        )

    input_images = images
    if edit_previous and not input_images:
        latest_export = _latest_sketch_export(ctx)
        if latest_export is None:
            return error_message(
                runtime,
                "No previous sketch image found in this thread to edit. Create a sketch first or provide an image.",
            )
        input_images = [latest_export]

    image_prompt = f"{IMAGE_PROMPT_TEMPLATE}\n\nUser request:\n{prompt.strip()}"
    storage_container = get_storage_container()
    image_buffers, mask_buffer, fetch_error = _fetch_input_buffers(
        storage_container, ctx, input_images, mask
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
                "prompt": image_prompt,
                "quality": quality,
                "size": DEFAULT_SIZE,
            }
            if mask_buffer:
                edit_kwargs["mask"] = mask_buffer
            result = client.images.edit(**edit_kwargs)
        else:
            result = client.images.generate(
                model="gpt-image-2",
                prompt=image_prompt,
                quality=quality,
                size=DEFAULT_SIZE,
            )
    except Exception as exc:
        return error_message(runtime, f"Image generation failed: {exc}")

    image_base64 = result.data[0].b64_json
    if not image_base64:
        return error_message(runtime, "Image generation returned no image data.")
    image_bytes = base64.b64decode(image_base64)

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return error_message(runtime, f"Generated image is not a valid PNG: {exc}")

    try:
        return _store_sketch(
            runtime,
            ctx=ctx,
            prompt=image_prompt,
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


def _latest_sketch_export(ctx: ToolContext) -> str | None:
    """Return the newest stored sketch PNG export for this design thread."""
    tables = get_design_tables()
    revisions = list_design_revisions(
        tables.design_revisions,
        ctx.owner_id,
        ctx.design_id,
    )
    for revision in reversed(revisions):
        try:
            exports = json.loads(revision.get("exports_json") or "[]")
        except json.JSONDecodeError:
            continue
        for export in exports:
            if (
                export.get("filename") == "sketch.png"
                and export.get("content_type") == SKETCH_CONTENT_TYPE
                and export.get("blob_name")
            ):
                return export["blob_name"]

    return None


def _fetch_input_buffers(
    storage_container,
    ctx: ToolContext,
    images: list[str] | None,
    mask: str | None,
) -> tuple[list[ImageUpload], ImageUpload | None, str | None]:
    image_buffers: list[ImageUpload] = []
    for image_path in images or []:
        buffer, error = _download_validated_image(storage_container, image_path, ctx)
        if buffer is None:
            return [], None, error
        image_buffers.append(buffer)

    mask_buffer: ImageUpload | None = None
    if mask:
        mask_buffer, error = _download_validated_image(storage_container, mask, ctx)
        if mask_buffer is None:
            return [], None, error

    return image_buffers, mask_buffer, None


def _download_validated_image(
    storage_container,
    image_path: str,
    ctx: ToolContext,
) -> tuple[ImageUpload | None, str | None]:
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

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
            metadata = SUPPORTED_INPUT_IMAGE_FORMATS.get(image.format)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return (
            None,
            f"Refused image {image_path!r}: not a readable image ({exc}).",
        )

    if metadata is None:
        return (
            None,
            f"Refused image {image_path!r}: unsupported image format; supported formats are JPEG, PNG, and WEBP.",
        )

    filename, content_type = metadata
    return (filename, io.BytesIO(image_bytes), content_type), None
