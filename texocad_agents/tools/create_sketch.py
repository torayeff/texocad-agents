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

from texocad_agents.sketch_agent.prompt import IMAGE_PROMPT_TEMPLATE
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
IMAGE_MODEL = "gpt-image-2"
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
    filename: str = "sketch.png",
    images: list[str] | None = None,
    mask: str | None = None,
) -> Command:
    """Generate or edit an image and store it as a design revision.

    When images is empty or omitted, generates a new image from the prompt.
    When images is provided, edits or composes using those images as references.
    Images can be user-uploaded attachment paths, previous revision export paths,
    or any accessible blob path.

    Args:
        prompt: Detailed text description of the image to generate, or edit
            instructions when images are provided.
        runtime: LangGraph tool runtime for the current tool call.
        quality: Image quality - "low" for drafts, "medium", "high" for
            final output, or "auto". Defaults to "low".
        filename: Informative PNG filename for the stored sketch artifact.
            Defaults to "sketch.png".
        images: List of blob paths or URLs for input images. Can include
            user-uploaded attachments or previous revision exports. When
            provided, the model edits or uses them as reference.
        mask: Blob path or URL of a mask image (PNG with alpha channel).
            Transparent areas indicate where the model should edit. Applied
            to the first image. Only used when images is provided.

    Returns:
        LangGraph command containing either an error message or a stored sketch
        revision artifact.
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
    if mask and not images:
        return error_message(runtime, "A mask can only be used together with images.")
    sketch_filename, filename_error = _normalize_sketch_filename(filename)
    if filename_error:
        return error_message(runtime, filename_error)

    ctx = get_tool_context(runtime)
    if ctx is None:
        return error_message(
            runtime,
            "Refused: cannot create sketch artifacts without an authenticated owner and thread.",
        )

    image_prompt = f"{IMAGE_PROMPT_TEMPLATE}\n\nUser request:\n{prompt.strip()}"
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
                "model": IMAGE_MODEL,
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
                model=IMAGE_MODEL,
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
            sketch_filename=sketch_filename,
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
    sketch_filename: str,
    image_bytes: bytes,
    image_base64: str,
) -> Command:
    """Persist generated sketch artifacts and build the tool response.

    Args:
        runtime: LangGraph tool runtime for the current tool call.
        ctx: Authenticated tool context.
        prompt: Prompt text used to generate or edit the image.
        sketch_filename: User-facing PNG filename for the stored sketch artifact.
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
            ctx.owner_id, ctx.design_id, revision_number, "exports", sketch_filename
        ),
        image_bytes,
        filename=sketch_filename,
        content_type=SKETCH_CONTENT_TYPE,
        content_disposition=f'inline; filename="{sketch_filename}"',
    )
    render_blob = upload_blob(
        storage_container,
        revision_blob_name(
            ctx.owner_id, ctx.design_id, revision_number, "previews", sketch_filename
        ),
        image_bytes,
        filename=sketch_filename,
        content_type=RENDER_CONTENT_TYPE,
        content_disposition=f'inline; filename="{sketch_filename}"',
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
                f"revision_number: {revision_number}\n"
                f"sketch_image_blob: {export_blob.blob_name}"
            )
        ),
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


def _normalize_sketch_filename(filename: str) -> tuple[str | None, str | None]:
    """Validate the explicit sketch artifact filename."""

    normalized = filename.strip()
    if not normalized:
        return None, "Invalid filename: filename must not be empty."
    if "/" in normalized or "\\" in normalized:
        return None, "Invalid filename: filename must not include path separators."
    if any(char in normalized for char in {'"', "\r", "\n"}):
        return None, "Invalid filename: filename must not include quotes or newlines."
    if not normalized.lower().endswith(".png"):
        normalized = f"{normalized}.png"
    return normalized, None


def _fetch_input_buffers(
    storage_container,
    ctx: ToolContext,
    images: list[str] | None,
    mask: str | None,
) -> tuple[list[ImageUpload], ImageUpload | None, str | None]:
    """Download and validate all image-edit inputs.

    Args:
        storage_container: Azure blob container client used to fetch blobs.
        ctx: Authenticated tool context used for blob authorization.
        images: Optional list of input image blob paths or URLs.
        mask: Optional mask blob path or URL.

    Returns:
        Tuple of input image uploads, optional mask upload, and optional error
        message.
    """

    image_buffers: list[ImageUpload] = []
    first_image_size: tuple[int, int] | None = None
    for image_path in images or []:
        buffer, image_size, error = _download_validated_image(
            storage_container, image_path, ctx
        )
        if buffer is None:
            return [], None, error
        if first_image_size is None:
            first_image_size = image_size
        image_buffers.append(buffer)

    mask_buffer: ImageUpload | None = None
    if mask:
        mask_buffer, _, error = _download_validated_mask(
            storage_container, mask, ctx, first_image_size
        )
        if mask_buffer is None:
            return [], None, error

    return image_buffers, mask_buffer, None


def _download_validated_image(
    storage_container,
    image_path: str,
    ctx: ToolContext,
) -> tuple[ImageUpload | None, tuple[int, int] | None, str | None]:
    """Download an authorized image and validate its model upload format.

    Args:
        storage_container: Azure blob container client used to fetch the image.
        image_path: Blob path or URL for the input image.
        ctx: Authenticated tool context used for blob authorization.

    Returns:
        Tuple of model upload payload, image dimensions, and optional error
        message.
    """

    image_bytes, error = _download_authorized_blob(
        storage_container, image_path, ctx, kind="image"
    )
    if error:
        return None, None, error

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
            metadata = SUPPORTED_INPUT_IMAGE_FORMATS.get(image.format)
            image_size = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return (
            None,
            None,
            f"Refused image {image_path!r}: not a readable image ({exc}).",
        )

    if metadata is None:
        return (
            None,
            None,
            f"Refused image {image_path!r}: unsupported image format; "
            "supported formats are JPEG, PNG, and WEBP.",
        )

    filename, content_type = metadata
    return (filename, io.BytesIO(image_bytes), content_type), image_size, None


def _download_validated_mask(
    storage_container,
    mask_path: str,
    ctx: ToolContext,
    required_size: tuple[int, int] | None,
) -> tuple[ImageUpload | None, tuple[int, int] | None, str | None]:
    """Download an authorized mask and validate image-edit mask constraints.

    Args:
        storage_container: Azure blob container client used to fetch the mask.
        mask_path: Blob path or URL for the mask image.
        ctx: Authenticated tool context used for blob authorization.
        required_size: Required mask dimensions from the first input image, when
            available.

    Returns:
        Tuple of model upload payload, mask dimensions, and optional error
        message.
    """

    mask_bytes, error = _download_authorized_blob(
        storage_container, mask_path, ctx, kind="mask"
    )
    if error:
        return None, None, error

    try:
        with Image.open(io.BytesIO(mask_bytes)) as image:
            image.verify()
            image_format = image.format
            image_mode = image.mode
            mask_size = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return None, None, f"Refused mask {mask_path!r}: not a readable image ({exc})."

    if image_format != "PNG":
        return (
            None,
            None,
            "Refused mask: mask must be a PNG image with an alpha channel.",
        )
    if "A" not in image_mode:
        return None, None, "Refused mask: mask PNG must include an alpha channel."
    if required_size is not None and mask_size != required_size:
        return (
            None,
            None,
            "Refused mask: mask dimensions must match the first input image "
            f"({required_size[0]}x{required_size[1]}), got "
            f"{mask_size[0]}x{mask_size[1]}.",
        )

    return ("mask.png", io.BytesIO(mask_bytes), "image/png"), mask_size, None


def _download_authorized_blob(
    storage_container,
    blob_path: str,
    ctx: ToolContext,
    *,
    kind: str,
) -> tuple[bytes | None, str | None]:
    """Download a user-scoped blob after authorization.

    Args:
        storage_container: Azure blob container client used to fetch the blob.
        blob_path: Blob path or URL to authorize and download.
        ctx: Authenticated tool context used for blob authorization.
        kind: Human-readable input kind used in error messages.

    Returns:
        Tuple of downloaded bytes and optional error message.
    """

    blob_name = authorize_blob_access(blob_path, ctx)
    if blob_name is None:
        return None, f"Refused {kind} {blob_path!r}: not in this thread/user scope."

    try:
        return (
            storage_container.get_blob_client(blob_name).download_blob().readall(),
            None,
        )
    except Exception as exc:
        return None, f"Could not download {kind} from {blob_path!r}: {exc}"
