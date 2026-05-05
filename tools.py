import base64
import mimetypes
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

import fitz
from daytona import Daytona
from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from sandbox import SANDBOX_CONFIG, ensure_snapshot, run_code
from storage import (
    blob_path_from_url,
    create_artifact_revision,
    create_design_revision,
    ensure_design,
    get_cloud_storage,
    get_design_tables,
    get_storage_container,
    new_id,
    upload_export_artifact,
    upload_source_artifact,
)


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


@tool
def parse_file(blob_name: str, runtime: ToolRuntime) -> Command | list | str:
    """Parse an uploaded file from the current thread or authenticated user scope."""
    thread_id = runtime.config["configurable"]["thread_id"]
    auth_user = runtime.config["configurable"].get("langgraph_auth_user") or {}
    user_id = auth_user.get("identity")

    blob_name = blob_path_from_url(blob_name)
    allowed_prefixes: tuple[str, ...] = (f"threads/{thread_id}/",)
    if user_id:
        allowed_prefixes += (f"uploads/{user_id}/",)

    if not blob_name.startswith(allowed_prefixes):
        return (
            "Refused: blob path is outside this thread/user scope. "
            "Attachments must live under "
            f"'threads/{thread_id}/...'"
            + (f" or 'uploads/{user_id}/...'." if user_id else ".")
        )

    supported_image_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    render_dpi = 300

    storage = get_cloud_storage()
    filename = PurePosixPath(blob_name).name
    extension = PurePosixPath(filename).suffix.lower()
    mime_type, _ = mimetypes.guess_type(filename)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = Path(tmpdir) / filename
        storage.download(blob_name, local_path)
        raw_bytes = local_path.read_bytes()

    if extension == ".svg":
        svg_text = raw_bytes.decode("utf-8")
        summary = _summarize_svg(svg_text)
        file_path = f"/models/{PurePosixPath(filename).stem}.svg"
        return Command(
            update={
                "files": {file_path: svg_text},
                "messages": [
                    ToolMessage(
                        content=f"Saved SVG to {file_path}.\n{summary}",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    if mime_type == "application/pdf":
        document = fitz.open(stream=raw_bytes, filetype="pdf")
        blocks = []
        try:
            for index, page in enumerate(document, start=1):
                png_bytes = page.get_pixmap(dpi=render_dpi).tobytes("png")
                png_b64 = base64.b64encode(png_bytes).decode("utf-8")
                blocks.append(create_text_block(text=f"Page {index}:"))
                blocks.append(create_image_block(base64=png_b64, mime_type="image/png"))
        finally:
            document.close()
        n_pages = len(blocks) // 2
        blocks.insert(
            0,
            create_text_block(
                text=f"Read '{blob_name}' - {n_pages} page(s) at {render_dpi} DPI:"
            ),
        )
        return blocks

    if mime_type and mime_type.startswith("image/"):
        if extension not in supported_image_types:
            return (
                f"Unsupported image format '{extension}'. "
                "Only .jpg, .jpeg, .png, and .webp images can be analyzed visually."
            )
        media = supported_image_types[extension]
        img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
        return [
            create_text_block(text=f"Read '{blob_name}':"),
            create_image_block(base64=img_b64, mime_type=media),
        ]

    return f"Read '{blob_name}':\n\n{raw_bytes.decode('utf-8')}"


def _runtime_config(runtime: ToolRuntime) -> dict[str, Any]:
    return runtime.config.get("configurable", {})


def _runtime_metadata(runtime: ToolRuntime) -> dict[str, Any]:
    return runtime.config.get("metadata", {})


def _runtime_owner_id(runtime: ToolRuntime) -> str | None:
    auth_user = _runtime_config(runtime).get("langgraph_auth_user") or {}
    return auth_user.get("identity") or _runtime_metadata(runtime).get("owner")


def _runtime_thread_id(runtime: ToolRuntime) -> str | None:
    return _runtime_config(runtime).get("thread_id")


def _runtime_run_id(runtime: ToolRuntime) -> str | None:
    config = _runtime_config(runtime)
    metadata = _runtime_metadata(runtime)
    return config.get("run_id") or metadata.get("run_id")


def _file_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, dict):
        for key in ("content", "text", "data"):
            content = value.get(key)
            if isinstance(content, str):
                return content
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    return None


def _read_runtime_file(file_path: str, runtime: ToolRuntime) -> str | None:
    files = runtime.state.get("files", {}) if isinstance(runtime.state, dict) else {}
    candidates = [file_path]
    if not file_path.startswith("/"):
        candidates.append(f"/{file_path}")

    for candidate in candidates:
        if candidate in files:
            return _file_text(files[candidate])
    return None


def _tool_summary(
    *,
    design_id: str,
    design_revision: dict,
    source_artifact_id: str,
    exports: list[dict[str, str]],
    sandbox_output: str,
) -> str:
    lines = [
        "Created design revision.",
        f"design_id: {design_id}",
        f"design_revision_id: {design_revision['design_revision_id']}",
        f"revision_number: {design_revision['revision_number']}",
        f"source_artifact_revision_id: {source_artifact_id}",
    ]
    if exports:
        lines.append("exports:")
        lines.extend(
            f"- {export['filename']}: {export['artifact_revision_id']}"
            for export in exports
        )
    else:
        lines.append("exports: none")
    if sandbox_output.strip():
        lines.append("sandbox_output:")
        lines.append(sandbox_output.strip()[:4000])
    return "\n".join(lines)


@tool
def execute_model(
    file_path: str,
    runtime: ToolRuntime,
    design_id: str | None = None,
    revision_name: str | None = None,
) -> str:

    code = _read_runtime_file(file_path, runtime)
    if code is None:
        return f"Could not find {file_path!r} in the agent filesystem."

    owner_id = _runtime_owner_id(runtime)
    if not owner_id:
        return "Refused: cannot create design artifacts without an authenticated owner."

    metadata = _runtime_metadata(runtime)
    design_id = design_id or metadata.get("design_id") or new_id("design")
    revision_name = revision_name or metadata.get("revision_name")
    thread_id = _runtime_thread_id(runtime)
    run_id = _runtime_run_id(runtime)

    tables = get_design_tables()
    storage_container = get_storage_container()
    ensure_design(
        tables.designs,
        owner_id,
        design_id,
        title=metadata.get("title") or PurePosixPath(file_path).stem,
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )
    design_revision = create_design_revision(
        tables.design_revisions,
        owner_id,
        design_id,
        revision_name=revision_name,
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )
    revision_number = int(design_revision["revision_number"])
    design_revision_id = design_revision["design_revision_id"]

    source_artifact = upload_source_artifact(
        storage_container,
        owner_id,
        design_id,
        design_revision_id,
        revision_number,
        code,
    )
    create_artifact_revision(
        tables.artifact_revisions,
        owner_id,
        design_id,
        design_revision_id,
        source_artifact,
        filename=PurePosixPath(file_path).name,
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )

    daytona = Daytona()
    ensure_snapshot(daytona, SANDBOX_CONFIG)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_code(daytona, code, Path(tmpdir), SANDBOX_CONFIG)

        if result.exit_code != 0:
            return (
                "Sandbox execution failed.\n"
                f"design_id: {design_id}\n"
                f"design_revision_id: {design_revision_id}\n"
                f"source_artifact_revision_id: {source_artifact.artifact_revision_id}\n"
                f"exit_code: {result.exit_code}\n"
                f"output:\n{result.output.strip()}"
            )

        exports = []
        for model_path in result.model_paths:
            export_artifact = upload_export_artifact(
                storage_container,
                owner_id,
                design_id,
                design_revision_id,
                revision_number,
                model_path,
            )
            create_artifact_revision(
                tables.artifact_revisions,
                owner_id,
                design_id,
                design_revision_id,
                export_artifact,
                filename=model_path.name,
                created_by_thread_id=thread_id,
                created_by_run_id=run_id,
            )
            exports.append({
                "filename": model_path.name,
                "artifact_revision_id": export_artifact.artifact_revision_id,
            })

    return _tool_summary(
        design_id=design_id,
        design_revision=design_revision,
        source_artifact_id=source_artifact.artifact_revision_id,
        exports=exports,
        sandbox_output=result.output,
    )
