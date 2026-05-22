import base64
import hashlib
import io
import tempfile
from pathlib import Path

from azure.storage.blob import ContentSettings
from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from openai import OpenAI

from texocad_agents.storage import (
    StoredBlob,
    blob_path_from_url,
    create_design_revision,
    ensure_design,
    get_design_tables,
    get_storage_container,
    revision_blob_name,
    save_design_preview,
    save_design_revision_blobs,
    upload_preview_blob,
    user_scope_hash,
)
from texocad_agents.tools.tool_runtime import error_tool_message

SKETCH_CONTENT_TYPE = "image/png"
SOURCE_CONTENT_TYPE = "text/plain; charset=utf-8"
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
def generate_sketch(
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
        quality: Image quality - "low", "medium", "high", or "auto".
        size: Image dimensions - "1024x1024", "1536x1024", "1024x1536", "2048x2048", or "auto".
        images: List of blob paths or URLs for input images. Can include user-uploaded attachments or previous revision exports. When provided, the model edits or uses them as reference.
        mask: Blob path or URL of a mask image (PNG with alpha channel). Transparent areas indicate where the model should edit. Applied to the first image. Only used when images is provided.
    """
    if quality not in ALLOWED_QUALITIES:
        return error_tool_message(
            runtime,
            f"Invalid quality {quality!r}. Must be one of: {', '.join(sorted(ALLOWED_QUALITIES))}.",
        )
    if size not in ALLOWED_SIZES:
        return error_tool_message(
            runtime,
            f"Invalid size {size!r}. Must be one of: {', '.join(sorted(ALLOWED_SIZES))}.",
        )
    if images and len(images) > MAX_INPUT_IMAGES:
        return error_tool_message(
            runtime,
            f"Too many input images ({len(images)}). Maximum is {MAX_INPUT_IMAGES}.",
        )
    if mask and not images:
        return error_tool_message(
            runtime,
            "A mask can only be used together with images.",
        )

    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity")
    if not owner_id:
        return error_tool_message(
            runtime,
            "Refused: cannot create sketch artifacts without an authenticated owner.",
        )

    raw_thread_id = configurable.get("thread_id")
    if not raw_thread_id:
        return error_tool_message(
            runtime,
            "Refused: cannot create sketch artifacts without a LangGraph thread_id.",
        )

    thread_id = str(raw_thread_id)
    design_id = thread_id
    run_id = configurable.get("run_id") or metadata.get("run_id")

    tables = get_design_tables()
    storage_container = get_storage_container()

    input_image_buffers: list[io.BytesIO] = []
    if images:
        for image_path in images:
            image_bytes = _download_image(
                storage_container, image_path, owner_id, thread_id
            )
            if image_bytes is None:
                return error_tool_message(
                    runtime,
                    f"Could not download image from {image_path!r}. "
                    "Verify it is a valid blob path accessible in this thread.",
                )
            input_image_buffers.append(io.BytesIO(image_bytes))

    mask_buffer: io.BytesIO | None = None
    if mask:
        mask_bytes = _download_image(storage_container, mask, owner_id, thread_id)
        if mask_bytes is None:
            return error_tool_message(
                runtime,
                f"Could not download mask from {mask!r}. "
                "Verify it is a valid blob path accessible in this thread.",
            )
        mask_buffer = io.BytesIO(mask_bytes)

    client = OpenAI()
    try:
        if input_image_buffers:
            edit_kwargs: dict = {
                "model": "gpt-image-2",
                "image": input_image_buffers
                if len(input_image_buffers) > 1
                else input_image_buffers[0],
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
        return error_tool_message(
            runtime,
            f"Image generation failed: {exc}",
        )

    image_base64 = result.data[0].b64_json
    if not image_base64:
        return error_tool_message(runtime, "Image generation returned no image data.")

    image_bytes = base64.b64decode(image_base64)

    ensure_design(
        tables.designs,
        owner_id,
        design_id,
        title=metadata.get("title") or "Sketch",
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )
    design_revision = create_design_revision(
        tables.design_revisions,
        owner_id,
        design_id,
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )
    revision_number = int(design_revision["revision_number"])

    source_blob = _upload_prompt_source(
        storage_container, owner_id, design_id, revision_number, prompt
    )
    export_blob = _upload_sketch_blob(
        storage_container, owner_id, design_id, revision_number, image_bytes
    )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)

    try:
        preview_blob = upload_preview_blob(
            storage_container,
            owner_id,
            design_id,
            revision_number,
            tmp_path,
            "sketch",
        )
        save_design_preview(
            tables.design_previews,
            owner_id,
            design_id,
            revision_number,
            "sketch",
            preview_blob,
            source_export_checksum=export_blob.checksum,
            camera=None,
            up=None,
            created_by_thread_id=thread_id,
            created_by_run_id=run_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    design_revision = save_design_revision_blobs(
        tables.design_revisions,
        design_revision,
        source_blob,
        [export_blob],
    )

    content_blocks = [
        create_text_block(
            text=(
                "Created sketch revision.\n"
                f"design_id: {design_id}\n"
                f"revision_number: {revision_number}"
            )
        ),
        create_text_block(text="sketch preview"),
        create_image_block(base64=image_base64, mime_type="image/png"),
    ]

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content_blocks,
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": {
                            "type": "texocad.sketch_revision",
                            "designId": design_id,
                            "revisionNumber": revision_number,
                            "source": _blob_payload(source_blob),
                            "exports": [_blob_payload(export_blob)],
                            "previews": [_blob_payload(preview_blob)],
                            "createdByThreadId": thread_id,
                            **({"createdByRunId": str(run_id)} if run_id else {}),
                        }
                    },
                )
            ]
        }
    )


def _download_image(
    storage_container,
    image_path: str,
    owner_id: str,
    thread_id: str,
) -> bytes | None:
    blob_name = blob_path_from_url(image_path)

    user_scope = f"users/{user_scope_hash(owner_id)}"
    allowed_prefixes = (
        f"{user_scope}/threads/{thread_id}/",
        f"{user_scope}/uploads/",
        f"{user_scope}/designs/",
    )
    if not blob_name.startswith(allowed_prefixes):
        return None

    try:
        return storage_container.get_blob_client(blob_name).download_blob().readall()
    except Exception:
        return None


def _upload_prompt_source(
    storage_container,
    owner_id: str,
    design_id: str,
    revision_number: int,
    prompt: str,
) -> StoredBlob:
    filename = "prompt.txt"
    blob_name = revision_blob_name(
        owner_id, design_id, revision_number, "source", filename
    )
    data = prompt.encode("utf-8")
    storage_container.get_blob_client(blob_name).upload_blob(
        data,
        overwrite=False,
        content_settings=ContentSettings(content_type=SOURCE_CONTENT_TYPE),
    )
    return StoredBlob(
        filename=filename,
        blob_name=blob_name,
        content_type=SOURCE_CONTENT_TYPE,
        checksum=hashlib.sha256(data).hexdigest(),
    )


def _upload_sketch_blob(
    storage_container,
    owner_id: str,
    design_id: str,
    revision_number: int,
    image_bytes: bytes,
) -> StoredBlob:
    filename = "sketch.png"
    blob_name = revision_blob_name(
        owner_id, design_id, revision_number, "exports", filename
    )
    storage_container.get_blob_client(blob_name).upload_blob(
        image_bytes,
        overwrite=False,
        content_settings=ContentSettings(
            content_type=SKETCH_CONTENT_TYPE,
            content_disposition=f'inline; filename="{filename}"',
        ),
    )
    return StoredBlob(
        filename=filename,
        blob_name=blob_name,
        content_type=SKETCH_CONTENT_TYPE,
        checksum=hashlib.sha256(image_bytes).hexdigest(),
    )


def _blob_payload(blob: StoredBlob) -> dict:
    return {
        "filename": blob.filename,
        "blobName": blob.blob_name,
        "contentType": blob.content_type,
        "checksum": blob.checksum,
    }
