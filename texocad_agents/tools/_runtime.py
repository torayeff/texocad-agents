import posixpath
from dataclasses import dataclass

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.storage import (
    StoredBlob,
    blob_path_from_url,
    create_design_revision,
    ensure_design,
    get_design_tables,
    get_storage_container,
    scope_hash,
)


@dataclass(frozen=True)
class ToolContext:
    owner_id: str
    thread_id: str
    design_id: str
    run_id: str | None
    metadata: dict


def tool_context(runtime: ToolRuntime) -> ToolContext | None:
    """Extract authenticated owner/thread context from runtime config.

    Returns None if owner_id or thread_id are missing (caller should
    return an error_message). One design per thread: design_id == thread_id.
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


def read_runtime_file(file_path: str, runtime: ToolRuntime) -> str | None:
    if not isinstance(runtime.state, dict):
        return None
    files = runtime.state.get("files", {})
    if not isinstance(files, dict):
        return None

    for candidate in (file_path, normalize_path(file_path)):
        if candidate not in files:
            continue
        value = files[candidate]
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


def authorize_blob_access(blob_path: str, ctx: ToolContext) -> str | None:
    """Return the resolved blob_name if the caller owns it, else None.

    Per-user boundary: any blob under `users/<scope_hash(owner_id)>/` is
    accessible. Path traversal (`..`) and absolute paths are rejected.
    Cross-user access is not supported.
    """
    raw_name = blob_path_from_url(blob_path)
    blob_name = posixpath.normpath(raw_name)
    if blob_name.startswith("/") or ".." in blob_name.split("/"):
        return None
    if not blob_name.startswith(f"users/{scope_hash(ctx.owner_id)}/"):
        return None
    return blob_name


def error_message(runtime: ToolRuntime, content: str) -> Command:
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


def create_revision(
    ctx: ToolContext, *, title: str
) -> tuple[object, object, dict, int]:
    """Ensure design exists and create a new revision.

    Returns (tables, storage_container, design_revision, revision_number).
    """
    tables = get_design_tables()
    storage_container = get_storage_container()
    ensure_design(
        tables.designs,
        ctx.owner_id,
        ctx.design_id,
        title=title,
        created_by_thread_id=ctx.thread_id,
        created_by_run_id=ctx.run_id,
    )
    design_revision = create_design_revision(
        tables.design_revisions,
        ctx.owner_id,
        ctx.design_id,
        created_by_thread_id=ctx.thread_id,
        created_by_run_id=ctx.run_id,
    )
    revision_number = int(design_revision["revision_number"])
    return tables, storage_container, design_revision, revision_number


def revision_artifact(
    *,
    artifact_type: str,
    design_id: str,
    design_revision: dict,
    source_blob: StoredBlob,
    exports: list[StoredBlob],
    renders: list[StoredBlob],
    thread_id: str,
    run_id: str | None,
) -> dict:
    """Build the texocad_artifact dict attached to ToolMessage additional_kwargs."""
    payload = {
        "type": artifact_type,
        "designId": design_id,
        "revisionNumber": int(design_revision["revision_number"]),
        "source": blob_payload(source_blob),
        "exports": [blob_payload(export) for export in exports],
        "renders": [blob_payload(render) for render in renders],
        "createdByThreadId": thread_id,
    }
    if run_id:
        payload["createdByRunId"] = str(run_id)
    return payload
