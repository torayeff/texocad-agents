import base64
import io
import mimetypes
from pathlib import PurePosixPath

import fitz
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from PIL import Image, UnidentifiedImageError

from texocad_agents.storage import get_storage_container
from texocad_agents.tools._runtime import (
    authorize_blob_access,
    error_message,
    get_tool_context,
)

PDF_RENDER_DPI = 300
MAX_TEXT_BYTES = 200 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PDF_RENDER_PIXELS = 30_000_000
MAX_PDF_TOTAL_PIXELS = 100_000_000
PDF_MAGIC = b"%PDF-"
SUPPORTED_IMAGE_FORMATS: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


@tool(parse_docstring=True)
def read_attachment(attachment_path: str, runtime: ToolRuntime) -> Command | list | str:
    """Read an uploaded design reference or attachment.

    Use this when the user asks about an uploaded image, PDF, or text file.
    The attachment must belong to the current thread or authenticated user scope.

    Args:
        attachment_path: Blob path or URL for the uploaded attachment.
    """
    ctx = get_tool_context(runtime)
    if ctx is None:
        return error_message(
            runtime,
            "Refused: cannot read attachments without an authenticated user.",
        )

    blob_name = authorize_blob_access(attachment_path, ctx)
    if blob_name is None:
        return error_message(
            runtime,
            "Refused: blob path is outside this user's scope.",
        )

    attachment = PurePosixPath(blob_name)
    filename = attachment.name
    extension = attachment.suffix.lower()
    mime_type, _ = mimetypes.guess_type(filename)

    if extension == ".svg" or mime_type == "image/svg+xml":
        return error_message(
            runtime,
            "Refused: SVG attachments are not supported.",
        )

    blob_client = get_storage_container().get_blob_client(blob_name)
    blob_size = blob_client.get_blob_properties().size
    size_error = _validate_attachment_size(blob_name, mime_type, blob_size)
    if size_error:
        return error_message(runtime, size_error)

    raw_bytes = blob_client.download_blob().readall()

    if raw_bytes.startswith(PDF_MAGIC) or mime_type == "application/pdf":
        pdf_result = _read_pdf(blob_name, raw_bytes)
        if isinstance(pdf_result, str):
            return error_message(runtime, pdf_result)
        return pdf_result

    image_result = _read_image_if_supported(blob_name, raw_bytes)
    if image_result is not None:
        if isinstance(image_result, str):
            return error_message(runtime, image_result)
        return image_result

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return error_message(
            runtime,
            f"Unsupported text encoding for '{blob_name}'. Attachments must be UTF-8.",
        )

    return f"Read '{blob_name}':\n\n{text}"


def _read_pdf(blob_name: str, raw_bytes: bytes) -> list | str:
    if not raw_bytes.startswith(PDF_MAGIC):
        return f"Refused: '{blob_name}' is not a valid PDF (missing %PDF- header)."

    try:
        document = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as exc:
        return f"Refused: '{blob_name}' is not a readable PDF ({exc})."

    try:
        n_pages = document.page_count
        if n_pages > MAX_PDF_PAGES:
            return (
                f"Refused: '{blob_name}' has {n_pages} pages, which exceeds "
                f"the {MAX_PDF_PAGES} page limit."
            )

        scale = PDF_RENDER_DPI / 72
        page_pixels = [
            int(page.rect.width * scale * page.rect.height * scale) for page in document
        ]
        for index, pixels in enumerate(page_pixels, start=1):
            if pixels > MAX_PDF_RENDER_PIXELS:
                return (
                    f"Refused: page {index} of '{blob_name}' would render to "
                    f"{pixels} pixels, which exceeds the "
                    f"{MAX_PDF_RENDER_PIXELS} pixel per-page limit."
                )
        total_pixels = sum(page_pixels)
        if total_pixels > MAX_PDF_TOTAL_PIXELS:
            return (
                f"Refused: '{blob_name}' would render to {total_pixels} total "
                f"pixels across {n_pages} pages, which exceeds the "
                f"{MAX_PDF_TOTAL_PIXELS} pixel total limit."
            )

        blocks = [
            create_text_block(
                text=f"Read '{blob_name}' - {n_pages} page(s) at {PDF_RENDER_DPI} DPI:"
            )
        ]
        for index, page in enumerate(document, start=1):
            png_bytes = page.get_pixmap(dpi=PDF_RENDER_DPI).tobytes("png")
            png_b64 = base64.b64encode(png_bytes).decode("utf-8")
            blocks.append(create_text_block(text=f"Page {index}:"))
            blocks.append(create_image_block(base64=png_b64, mime_type="image/png"))
        return blocks
    finally:
        document.close()


def _validate_attachment_size(
    blob_name: str,
    mime_type: str | None,
    blob_size: int,
) -> str | None:
    max_bytes = MAX_TEXT_BYTES
    if mime_type == "application/pdf":
        max_bytes = MAX_PDF_BYTES
    elif mime_type and mime_type.startswith("image/"):
        max_bytes = MAX_IMAGE_BYTES

    if blob_size <= max_bytes:
        return None

    return (
        f"Refused: '{blob_name}' is {blob_size} bytes, which exceeds "
        f"the {max_bytes} byte limit for this attachment type."
    )


def _read_image_if_supported(blob_name: str, raw_bytes: bytes) -> list | str | None:
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    image_format = (image.format or "").upper()
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        return (
            f"Unsupported image format '{image_format or 'unknown'}'. "
            "Only PNG, JPEG, and WEBP images can be analyzed visually."
        )

    img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    return [
        create_text_block(text=f"Read '{blob_name}':"),
        create_image_block(
            base64=img_b64,
            mime_type=SUPPORTED_IMAGE_FORMATS[image_format],
        ),
    ]
