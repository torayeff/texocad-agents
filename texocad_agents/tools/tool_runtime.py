from dataclasses import dataclass

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.storage import StoredBlob


@dataclass(frozen=True)
class ToolContext:
    owner_id: str
    thread_id: str
    design_id: str
    run_id: str | None
    metadata: dict


def resolve_tool_context(runtime: ToolRuntime) -> ToolContext | None:
    """Extract authenticated owner/thread context from runtime config.

    Returns None if owner_id or thread_id are missing (caller should
    return an error_tool_message).
    """
    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity")
    if not owner_id:
        return None

    raw_thread_id = configurable.get("thread_id")
    if not raw_thread_id:
        return None

    thread_id = str(raw_thread_id)
    run_id = configurable.get("run_id") or metadata.get("run_id")

    return ToolContext(
        owner_id=owner_id,
        thread_id=thread_id,
        design_id=thread_id,
        run_id=run_id,
        metadata=metadata,
    )


def blob_payload(blob: StoredBlob) -> dict:
    """Convert a StoredBlob to a camelCase dict for artifact payloads."""
    return {
        "filename": blob.filename,
        "blobName": blob.blob_name,
        "contentType": blob.content_type,
        "checksum": blob.checksum,
    }


def normalize_path(file_path: str) -> str:
    if file_path.startswith("/"):
        return file_path
    return f"/{file_path}"


def runtime_files(runtime: ToolRuntime) -> dict:
    if not isinstance(runtime.state, dict):
        return {}

    files = runtime.state.get("files", {})
    if isinstance(files, dict):
        return files
    return {}


def read_runtime_file(file_path: str, runtime: ToolRuntime) -> str | None:
    files = runtime_files(runtime)

    for candidate in (file_path, normalize_path(file_path)):
        if candidate in files:
            return file_text(files[candidate])
    return None


def file_text(value: object) -> str | None:
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


def error_tool_message(runtime: ToolRuntime, content: str) -> Command:
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content,
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            ]
        }
    )
