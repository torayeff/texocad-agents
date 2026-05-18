import tempfile
from dataclasses import asdict
from pathlib import Path

from daytona import Daytona
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.runtime_files import read_runtime_file, support_files_for
from texocad_agents.sandbox import SANDBOX_CONFIG, ensure_snapshot, run_code
from texocad_agents.storage import (
    create_design_revision,
    ensure_design,
    get_design_tables,
    get_storage_container,
    save_design_revision_blobs,
    upload_export_blob,
    upload_source_blob,
)


@tool(parse_docstring=True)
def build_design(
    file_path: str,
    runtime: ToolRuntime,
) -> Command | str:
    """Build CAD code and store a design revision with exported model files.

    Use this after writing or editing a CAD Python file in the agent filesystem.
    The tool saves the source, executes it in a sandbox, uploads generated model
    exports, and returns the stored revision and blob paths. Each LangGraph
    thread maps to exactly one design; every successful build in the same thread
    creates a new revision for that design.

    Args:
        file_path: Path to the CAD Python file in the agent filesystem.
    """

    code = read_runtime_file(file_path, runtime)
    if code is None:
        return f"Could not find {file_path!r} in the agent filesystem."

    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity")
    if not owner_id:
        return "Refused: cannot create design artifacts without an authenticated owner."

    raw_thread_id = configurable.get("thread_id")
    if not raw_thread_id:
        return "Refused: cannot create design artifacts without a LangGraph thread_id."

    thread_id = str(raw_thread_id)
    design_id = thread_id
    run_id = configurable.get("run_id") or metadata.get("run_id")

    daytona = Daytona()
    ensure_snapshot(daytona, SANDBOX_CONFIG)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_code(
            daytona,
            code,
            Path(tmpdir),
            SANDBOX_CONFIG,
            support_files=support_files_for(file_path, runtime),
        )

        if result.exit_code != 0:
            return (
                "Sandbox execution failed.\n"
                f"design_id: {design_id}\n"
                "revision_created: false\n"
                f"exit_code: {result.exit_code}\n"
                f"output:\n{result.output.strip()}"
            )

        tables = get_design_tables()
        storage_container = get_storage_container()
        ensure_design(
            tables.designs,
            owner_id,
            design_id,
            title=metadata.get("title") or Path(file_path).stem,
            created_by_thread_id=thread_id,
            created_by_run_id=run_id,
        )
        design_revision = create_design_revision(
            tables.design_revisions,
            owner_id,
            design_id,
            created_by_thread_id=thread_id,
            created_by_run_id=run_id,
        )
        revision_number = int(design_revision["revision_number"])
        source_blob = upload_source_blob(
            storage_container,
            owner_id,
            design_id,
            revision_number,
            Path(file_path).name,
            code,
        )
        exports = [
            upload_export_blob(
                storage_container,
                owner_id,
                design_id,
                revision_number,
                model_path,
            )
            for model_path in result.model_paths
        ]

    design_revision = save_design_revision_blobs(
        tables.design_revisions,
        design_revision,
        source_blob,
        exports,
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=(
                        "Created design revision.\n"
                        f"design_id: {design_id}\n"
                        f"revision_number: {design_revision['revision_number']}"
                    ),
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": _revision_artifact_payload(
                            design_id=design_id,
                            design_revision=design_revision,
                            source_blob=source_blob,
                            exports=exports,
                            thread_id=thread_id,
                            run_id=run_id,
                        )
                    },
                )
            ]
        }
    )


def _revision_artifact_payload(
    *,
    design_id: str,
    design_revision: dict,
    source_blob,
    exports: list,
    thread_id: str,
    run_id: str | None,
) -> dict:
    payload = {
        "type": "texocad.design_revision",
        "designId": design_id,
        "revisionNumber": int(design_revision["revision_number"]),
        "source": _stored_blob_payload(source_blob),
        "exports": [_stored_blob_payload(export) for export in exports],
        "createdByThreadId": thread_id,
    }
    if run_id:
        payload["createdByRunId"] = str(run_id)
    return payload


def _stored_blob_payload(blob) -> dict:
    data = asdict(blob)
    return {
        "filename": data["filename"],
        "blobName": data["blob_name"],
        "contentType": data["content_type"],
        "checksum": data["checksum"],
    }
