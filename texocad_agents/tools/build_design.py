import tempfile
from dataclasses import asdict
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.sandbox import run_python
from texocad_agents.storage import (
    create_design_revision,
    ensure_design,
    get_design_tables,
    get_storage_container,
    save_design_revision_blobs,
    upload_export_blob,
    upload_source_blob,
)
from texocad_agents.tools.preview_design import (
    build_preview_command,
    save_preview,
)
from texocad_agents.tools.tool_runtime import (
    error_tool_message,
    read_runtime_file,
)

MODEL_EXTENSION = ".step"
MAX_MODEL_FILES = 1
MAX_MODEL_BYTES = 100 * 1024 * 1024


@tool(parse_docstring=True)
def build_design(
    file_path: str,
    runtime: ToolRuntime,
) -> Command:
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
        return error_tool_message(
            runtime,
            f"Could not find {file_path!r} in the agent filesystem.",
        )

    configurable = runtime.config.get("configurable", {})
    metadata = runtime.config.get("metadata", {})
    auth_user = configurable.get("langgraph_auth_user") or {}

    owner_id = auth_user.get("identity")
    if not owner_id:
        return error_tool_message(
            runtime,
            "Refused: cannot create design artifacts without an authenticated owner.",
        )

    raw_thread_id = configurable.get("thread_id")
    if not raw_thread_id:
        return error_tool_message(
            runtime,
            "Refused: cannot create design artifacts without a LangGraph thread_id.",
        )

    thread_id = str(raw_thread_id)
    design_id = thread_id
    run_id = configurable.get("run_id") or metadata.get("run_id")

    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_python(
            code,
            Path(tmpdir),
            post_run=build_preview_command(),
        )

        if result.exit_code != 0:
            return _sandbox_failure_command(
                runtime,
                design_id=design_id,
                exit_code=result.exit_code,
                output=result.output,
            )

        step_files_or_error = _validated_step_files(result.files)
        if isinstance(step_files_or_error, str):
            return _sandbox_failure_command(
                runtime,
                design_id=design_id,
                exit_code=1,
                output=step_files_or_error,
            )
        step_files = step_files_or_error
        preview_files = _preview_files(result.files)

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
                step_file,
            )
            for step_file in step_files
        ]

        previews: dict[str, dict] = {}
        preview_errors: list[str] = []
        for view, image_path in preview_files.items():
            try:
                previews[view] = save_preview(
                    tables.design_previews,
                    storage_container,
                    owner_id=owner_id,
                    design_id=design_id,
                    revision_number=revision_number,
                    image_path=image_path,
                    view=view,
                    source_export_checksum=exports[0].checksum,
                    camera=None,
                    up=None,
                    thread_id=thread_id,
                    run_id=run_id,
                )
            except Exception as exc:
                preview_errors.append(f"{view}: {exc}")

    design_revision = save_design_revision_blobs(
        tables.design_revisions,
        design_revision,
        source_blob,
        exports,
    )
    preview_error = (
        "\npreview_failed: " + "; ".join(preview_errors) if preview_errors else ""
    )
    content_blocks = [
        create_text_block(
            text=(
                "Created design revision.\n"
                f"design_id: {design_id}\n"
                f"revision_number: {design_revision['revision_number']}"
                f"{preview_error}"
            )
        )
    ]
    for view, preview in previews.items():
        if preview.get("image"):
            content_blocks.extend([
                create_text_block(text=f"{view} preview"),
                create_image_block(
                    base64=preview["image"],
                    mime_type="image/png",
                ),
            ])

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content_blocks,
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": _revision_artifact_payload(
                            design_id=design_id,
                            design_revision=design_revision,
                            source_blob=source_blob,
                            exports=exports,
                            previews=[
                                blob
                                for preview in previews.values()
                                for blob in preview["blobs"]
                            ],
                            thread_id=thread_id,
                            run_id=run_id,
                        )
                    },
                )
            ]
        }
    )


def _validated_step_files(
    files: tuple[Path, ...],
) -> tuple[Path, ...] | str:
    unsupported_files = [
        file_path.name
        for file_path in files
        if file_path.suffix.lower() not in {MODEL_EXTENSION, ".png"}
    ]
    if unsupported_files:
        return (
            "Sandbox output must contain only .step files; found unsupported "
            f"file(s): {', '.join(unsupported_files)}."
        )

    step_files = tuple(
        file_path for file_path in files if file_path.suffix.lower() == MODEL_EXTENSION
    )
    if len(step_files) != MAX_MODEL_FILES:
        return (
            f"Sandbox must produce exactly {MAX_MODEL_FILES} .step file; "
            f"found {len(step_files)}."
        )

    for step_file in step_files:
        model_size = step_file.stat().st_size
        if model_size > MAX_MODEL_BYTES:
            return (
                f"Sandbox produced oversized model file {step_file.name!r}: "
                f"{model_size} bytes; limit is {MAX_MODEL_BYTES} bytes."
            )

    return step_files


def _preview_files(files: tuple[Path, ...]) -> dict[str, Path | None]:
    png_files = [f for f in files if f.suffix.lower() == ".png"]

    def by_suffix(suffix: str) -> Path | None:
        matches = [f for f in png_files if f.name.endswith(suffix)]
        return matches[0] if len(matches) == 1 else None

    return {
        "iso_ortho": by_suffix("_iso_ortho.png"),
        "iso_render": by_suffix("_iso_render.png"),
    }


def _sandbox_failure_command(
    runtime: ToolRuntime,
    *,
    design_id: str,
    exit_code: int,
    output: str,
) -> Command:
    return error_tool_message(
        runtime,
        "Sandbox execution failed.\n"
        f"design_id: {design_id}\n"
        "revision_created: false\n"
        f"exit_code: {exit_code}\n"
        f"output:\n{output.strip()}",
    )


def _revision_artifact_payload(
    *,
    design_id: str,
    design_revision: dict,
    source_blob,
    exports: list,
    previews: list,
    thread_id: str,
    run_id: str | None,
) -> dict:
    payload = {
        "type": "texocad.design_revision",
        "designId": design_id,
        "revisionNumber": int(design_revision["revision_number"]),
        "source": _stored_blob_payload(source_blob),
        "exports": [_stored_blob_payload(export) for export in exports],
        "previews": [_stored_blob_payload(preview) for preview in previews],
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
