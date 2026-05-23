import posixpath
from dataclasses import dataclass

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.storage import (
    StoredBlob,
    blob_path_from_url,
    blob_payload,
    create_design_revision,
    ensure_design,
    get_design_tables,
    get_storage_container,
    scope_hash,
)


@dataclass(frozen=True)
class ToolContext:
    """Authenticated context for a tool invocation.

    Attributes:
        owner_id: Authenticated user identity from LangGraph config.
        thread_id: LangGraph thread identifier for the current conversation.
        design_id: Stable design identifier. This currently matches `thread_id`.
        run_id: Optional LangGraph run identifier.
        metadata: Runtime metadata passed with the graph invocation.
    """

    owner_id: str
    thread_id: str
    design_id: str
    run_id: str | None
    metadata: dict


def tool_context(runtime: ToolRuntime) -> ToolContext | None:
    """Extract authenticated owner/thread context from runtime config.

    One design is associated with each LangGraph thread, so `design_id` is
    currently the same value as `thread_id`.

    Args:
        runtime: LangGraph tool runtime for the current tool call.

    Returns:
        Tool context when `owner_id` and `thread_id` are present; otherwise
        None. Callers should return an `error_message` when this is None.
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


def read_runtime_file(file_path: str, runtime: ToolRuntime) -> str | None:
    """Read a text file from the agent runtime filesystem state.

    Args:
        file_path: Absolute or relative path to the file in the agent
            filesystem.
        runtime: LangGraph tool runtime containing the current agent state.

    Returns:
        File content as text when the file exists in runtime state and can be
        decoded; otherwise None.
    """
    if not isinstance(runtime.state, dict):
        return None
    files = runtime.state.get("files", {})
    if not isinstance(files, dict):
        return None

    normalized = file_path if file_path.startswith("/") else f"/{file_path}"
    candidates = (file_path,) if normalized == file_path else (file_path, normalized)
    for candidate in candidates:
        if candidate not in files:
            continue
        value = files[candidate]
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, str):
                return content
    return None


def authorize_blob_access(blob_path: str, ctx: ToolContext) -> str | None:
    """Authorize access to a user-scoped blob path.

    Per-user boundary: any blob under `users/<scope_hash(owner_id)>/` is
    accessible. Path traversal (`..`), absolute paths, and cross-user access
    are rejected.

    Args:
        blob_path: Blob path or URL provided to a tool.
        ctx: Authenticated tool context for the current caller.

    Returns:
        Normalized blob name when the caller owns it; otherwise None.
    """
    raw_name = blob_path_from_url(blob_path)
    blob_name = posixpath.normpath(raw_name)
    if blob_name.startswith("/") or ".." in blob_name.split("/"):
        return None
    if not blob_name.startswith(f"users/{scope_hash(ctx.owner_id)}/"):
        return None
    return blob_name


def error_message(runtime: ToolRuntime, content: str) -> Command:
    """Build a LangGraph command containing an error tool message.

    Args:
        runtime: LangGraph tool runtime for the current tool call.
        content: Error text to return to the agent.

    Returns:
        Command that appends an error-status `ToolMessage`.
    """
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

    Args:
        ctx: Authenticated tool context for the current caller.
        title: Human-readable design title used when creating the design.

    Returns:
        Tuple of `(tables, storage_container, design_revision, revision_number)`.
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
    """Build artifact metadata for a design revision tool message.

    Args:
        artifact_type: Type identifier for the produced artifact.
        design_id: Stable design identifier for the current thread.
        design_revision: Persisted design revision row.
        source_blob: Stored source-code blob for the revision.
        exports: Stored export blobs, such as STEP files.
        renders: Stored render image blobs.
        thread_id: LangGraph thread identifier that created the artifact.
        run_id: Optional LangGraph run identifier that created the artifact.

    Returns:
        `texocad_artifact` payload suitable for `ToolMessage.additional_kwargs`.
    """
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
