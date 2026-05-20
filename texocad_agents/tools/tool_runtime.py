from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command


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
