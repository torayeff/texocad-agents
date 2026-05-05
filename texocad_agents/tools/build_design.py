import tempfile
from pathlib import Path, PurePosixPath

from daytona import Daytona
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from texocad_agents.sandbox import SANDBOX_CONFIG, ensure_snapshot, run_code
from texocad_agents.storage import (
    create_artifact_revision,
    create_design_revision,
    ensure_design,
    get_design_tables,
    get_storage_container,
    new_id,
    upload_export_artifact,
    upload_source_artifact,
)


@tool(parse_docstring=True)
def build_design(
    file_path: str,
    runtime: ToolRuntime,
    design_id: str | None = None,
    revision_name: str | None = None,
) -> str:
    """Build CAD code and store a design revision with exported model files.

    Use this after writing or editing a CAD Python file in the agent filesystem.
    The tool saves the source, executes it in a sandbox, uploads generated model
    exports, and returns the stored revision and artifact identifiers.

    Args:
        file_path: Path to the CAD Python file in the agent filesystem.
        design_id: Existing design ID to add a revision to. Leave empty to create one.
        revision_name: Optional human-readable name for this design revision.
    """

    code = _read_runtime_file(file_path, runtime)
    if code is None:
        return f"Could not find {file_path!r} in the agent filesystem."

    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity") or metadata.get("owner")
    if not owner_id:
        return "Refused: cannot create design artifacts without an authenticated owner."

    design_id = design_id or metadata.get("design_id") or new_id("design")
    revision_name = revision_name or metadata.get("revision_name")
    thread_id = configurable.get("thread_id")
    run_id = configurable.get("run_id") or metadata.get("run_id")

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
            exports.append(
                {
                    "filename": model_path.name,
                    "artifact_revision_id": export_artifact.artifact_revision_id,
                }
            )

    return _tool_summary(
        design_id=design_id,
        design_revision=design_revision,
        source_artifact_id=source_artifact.artifact_revision_id,
        exports=exports,
        sandbox_output=result.output,
    )


def _read_runtime_file(file_path: str, runtime: ToolRuntime) -> str | None:
    files = runtime.state.get("files", {}) if isinstance(runtime.state, dict) else {}
    candidates = [file_path]
    if not file_path.startswith("/"):
        candidates.append(f"/{file_path}")

    for candidate in candidates:
        if candidate in files:
            return _file_text(files[candidate])
    return None


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
