from pathlib import PurePosixPath

from langgraph.prebuilt.tool_node import ToolRuntime


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


def support_files_for(file_path: str, runtime: ToolRuntime) -> dict[str, bytes]:
    script_path = PurePosixPath(normalize_path(file_path))
    support_files: dict[str, bytes] = {}

    for candidate_path, value in runtime_files(runtime).items():
        attachment_path = PurePosixPath(normalize_path(str(candidate_path)))
        if attachment_path.parent != script_path.parent:
            continue
        if attachment_path.suffix.lower() != ".svg":
            continue

        content = file_text(value)
        if content is not None:
            support_files[attachment_path.name] = content.encode("utf-8")

    return support_files


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
