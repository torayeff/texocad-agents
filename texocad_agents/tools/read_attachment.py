import base64
import mimetypes
from pathlib import PurePosixPath

import fitz
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.storage import (
    get_storage_container,
)
from texocad_agents.tools.tool_runtime import authorize_blob_access, error_tool_message

SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
PDF_RENDER_DPI = 300
MAX_TEXT_BYTES = 200 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PDF_RENDER_PIXELS = 30_000_000


@tool(parse_docstring=True)
def read_attachment(attachment_path: str, runtime: ToolRuntime) -> Command | list | str:
    """Read an uploaded design reference or attachment.

    Use this when the user asks about an uploaded image, PDF, or text file.
    The attachment must belong to the current thread or authenticated user scope.

    Args:
        attachment_path: Blob path or URL for the uploaded attachment.
    """
    thread_id = runtime.config["configurable"]["thread_id"]
    auth_user = runtime.config["configurable"].get("langgraph_auth_user") or {}
    user_id = auth_user.get("identity")

    if not user_id:
        return error_tool_message(
            runtime,
            "Refused: cannot read attachments without an authenticated user.",
        )

    blob_name = authorize_blob_access(attachment_path, user_id, thread_id)
    if blob_name is None:
        return error_tool_message(
            runtime,
            (
                "Refused: blob path is outside this thread/user scope. "
                "Attachments must live under "
                f"'users/<hash>/threads/{thread_id}/...'"
                " or 'users/<hash>/uploads/...'."
            ),
        )

    attachment = PurePosixPath(blob_name)
    filename = attachment.name
    extension = attachment.suffix.lower()
    mime_type, _ = mimetypes.guess_type(filename)

    if extension == ".svg" or mime_type == "image/svg+xml":
        return error_tool_message(
            runtime,
            "Refused: SVG attachments are not supported.",
        )

    blob_client = get_storage_container().get_blob_client(blob_name)
    blob_size = blob_client.get_blob_properties().size
    size_error = _validate_attachment_size(blob_name, extension, mime_type, blob_size)
    if size_error:
        return error_tool_message(runtime, size_error)

    raw_bytes = blob_client.download_blob().readall()

    if mime_type == "application/pdf":
        pdf_result = _read_pdf(blob_name, raw_bytes)
        if isinstance(pdf_result, str):
            return error_tool_message(runtime, pdf_result)
        return pdf_result

    if mime_type and mime_type.startswith("image/"):
        image_result = _read_image(blob_name, extension, raw_bytes)
        if isinstance(image_result, str):
            return error_tool_message(runtime, image_result)
        return image_result

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return error_tool_message(
            runtime,
            f"Unsupported text encoding for '{blob_name}'. Attachments must be UTF-8.",
        )

    return f"Read '{blob_name}':\n\n{text}"


def _read_pdf(blob_name: str, raw_bytes: bytes) -> list | str:
    document = fitz.open(stream=raw_bytes, filetype="pdf")
    try:
        n_pages = document.page_count
        page_count_error = _validate_pdf_page_count(blob_name, n_pages)
        if page_count_error:
            return page_count_error

        blocks = [
            create_text_block(
                text=f"Read '{blob_name}' - {n_pages} page(s) at {PDF_RENDER_DPI} DPI:"
            )
        ]
        for index, page in enumerate(document, start=1):
            render_error = _validate_pdf_render_size(blob_name, index, page)
            if render_error:
                return render_error
            png_bytes = page.get_pixmap(dpi=PDF_RENDER_DPI).tobytes("png")
            png_b64 = base64.b64encode(png_bytes).decode("utf-8")
            blocks.append(create_text_block(text=f"Page {index}:"))
            blocks.append(create_image_block(base64=png_b64, mime_type="image/png"))
        return blocks
    finally:
        document.close()


def _validate_attachment_size(
    blob_name: str,
    extension: str,
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


def _validate_pdf_page_count(blob_name: str, n_pages: int) -> str | None:
    if n_pages <= MAX_PDF_PAGES:
        return None

    return (
        f"Refused: '{blob_name}' has {n_pages} pages, which exceeds "
        f"the {MAX_PDF_PAGES} page limit."
    )


def _validate_pdf_render_size(
    blob_name: str, page_index: int, page: fitz.Page
) -> str | None:
    scale = PDF_RENDER_DPI / 72
    pixels = int(page.rect.width * scale * page.rect.height * scale)
    if pixels <= MAX_PDF_RENDER_PIXELS:
        return None

    return (
        f"Refused: page {page_index} of '{blob_name}' would render to "
        f"{pixels} pixels, which exceeds the {MAX_PDF_RENDER_PIXELS} pixel limit."
    )


def _read_image(blob_name: str, extension: str, raw_bytes: bytes) -> list | str:
    if extension not in SUPPORTED_IMAGE_TYPES:
        return (
            f"Unsupported image format '{extension}'. "
            "Only .jpg, .jpeg, .png, and .webp images can be analyzed visually."
        )

    img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    return [
        create_text_block(text=f"Read '{blob_name}':"),
        create_image_block(
            base64=img_b64,
            mime_type=SUPPORTED_IMAGE_TYPES[extension],
        ),
    ]
