from langgraph.prebuilt.tool_node import ToolRuntime


def read_runtime_file(file_path: str, runtime: ToolRuntime) -> str | None:
    files = runtime.state.get("files", {}) if isinstance(runtime.state, dict) else {}
    candidates = [file_path]
    if not file_path.startswith("/"):
        candidates.append(f"/{file_path}")

    for candidate in candidates:
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
