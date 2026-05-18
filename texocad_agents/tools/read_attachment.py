import base64
import mimetypes
import re
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

import fitz
from deepagents.backends.utils import create_file_data
from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.storage import (
    blob_path_from_url,
    get_storage_container,
    user_scope_hash,
)

SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
PDF_RENDER_DPI = 300
MAX_TEXT_BYTES = 200 * 1024
MAX_SVG_BYTES = 1 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PDF_RENDER_PIXELS = 30_000_000


@tool(parse_docstring=True)
def read_attachment(attachment_path: str, runtime: ToolRuntime) -> Command | list | str:
    """Read an uploaded design reference or attachment.

    Use this when the user asks about an uploaded image, PDF, SVG, or text file.
    The attachment must belong to the current thread or authenticated user scope.

    Args:
        attachment_path: Blob path or URL for the uploaded attachment.
    """
    thread_id = runtime.config["configurable"]["thread_id"]
    auth_user = runtime.config["configurable"].get("langgraph_auth_user") or {}
    user_id = auth_user.get("identity")

    blob_name = blob_path_from_url(attachment_path)
    allowed_prefixes: tuple[str, ...]
    if user_id:
        user_scope = f"users/{user_scope_hash(user_id)}"
        allowed_prefixes = (
            f"{user_scope}/threads/{thread_id}/",
            f"{user_scope}/uploads/",
        )
    else:
        allowed_prefixes = ()

    if not blob_name.startswith(allowed_prefixes):
        return (
            "Refused: blob path is outside this thread/user scope. "
            "Attachments must live under "
            f"'users/<hash>/threads/{thread_id}/...'"
            + (" or 'users/<hash>/uploads/...'." if user_id else ".")
        )

    attachment = PurePosixPath(blob_name)
    filename = attachment.name
    extension = attachment.suffix.lower()
    mime_type, _ = mimetypes.guess_type(filename)

    blob_client = get_storage_container().get_blob_client(blob_name)
    blob_size = blob_client.get_blob_properties().size
    size_error = _validate_attachment_size(blob_name, extension, mime_type, blob_size)
    if size_error:
        return size_error

    raw_bytes = blob_client.download_blob().readall()

    if extension == ".svg":
        return _read_svg(filename, raw_bytes, runtime)

    if mime_type == "application/pdf":
        return _read_pdf(blob_name, raw_bytes)

    if mime_type and mime_type.startswith("image/"):
        return _read_image(blob_name, extension, raw_bytes)

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return (
            f"Unsupported text encoding for '{blob_name}'. Attachments must be UTF-8."
        )

    return f"Read '{blob_name}':\n\n{text}"


def _read_svg(filename: str, raw_bytes: bytes, runtime: ToolRuntime) -> Command:
    svg_text = raw_bytes.decode("utf-8")
    summary = _summarize_svg(svg_text)
    file_path = f"/models/{PurePosixPath(filename).stem}.svg"
    return Command(
        update={
            "files": {file_path: create_file_data(svg_text)},
            "messages": [
                ToolMessage(
                    content=f"Saved SVG to {file_path}.\n{summary}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )


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
    if extension == ".svg":
        max_bytes = MAX_SVG_BYTES
    elif mime_type == "application/pdf":
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


def _summarize_svg(svg_text: str) -> str:
    lines: list[str] = []

    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return "Could not parse SVG XML."

    viewbox = root.get("viewBox")
    if viewbox:
        lines.append(f"viewBox: {viewbox}")
    width = root.get("width")
    height = root.get("height")
    if width and height:
        lines.append(f"size: {width} x {height}")

    colors: list[str] = []
    path_count = 0
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "path":
            path_count += 1
        fill_attr = elem.get("fill")
        if fill_attr and fill_attr.lower() not in ("none", ""):
            colors.append(fill_attr.lower())
        style = elem.get("style", "")
        match = re.search(r"fill:\s*(#[0-9a-fA-F]{3,8})", style)
        if match:
            colors.append(match.group(1).lower())

    unique_colors = list(dict.fromkeys(colors))
    lines.append(f"paths: {path_count}")
    if unique_colors:
        lines.append(f"fill colors ({len(unique_colors)}): {', '.join(unique_colors)}")

    return "\n".join(lines)
