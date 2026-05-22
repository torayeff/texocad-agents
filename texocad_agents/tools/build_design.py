import tempfile
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.sandbox import run_python
from texocad_agents.storage import (
    upload_export_blob,
    upload_source_blob,
)
from texocad_agents.tools.preview_design import (
    build_preview_command,
    save_preview,
)
from texocad_agents.tools.tool_runtime import (
    create_revision,
    error_tool_message,
    finalize_revision,
    read_runtime_file,
    resolve_tool_context,
    revision_artifact_payload,
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

    ctx = resolve_tool_context(runtime)
    if ctx is None:
        return error_tool_message(
            runtime,
            "Refused: cannot create design artifacts without an authenticated owner and thread.",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_python(
            code,
            Path(tmpdir),
            post_run=build_preview_command(),
        )

        if result.exit_code != 0:
            return _sandbox_failure_command(
                runtime,
                design_id=ctx.design_id,
                exit_code=result.exit_code,
                output=result.output,
            )

        step_files_or_error = _validated_step_files(result.files)
        if isinstance(step_files_or_error, str):
            return _sandbox_failure_command(
                runtime,
                design_id=ctx.design_id,
                exit_code=1,
                output=step_files_or_error,
            )
        step_files = step_files_or_error
        preview_files = _preview_files(result.files)

        tables, storage_container, design_revision, revision_number = create_revision(
            ctx, title=ctx.metadata.get("title") or Path(file_path).stem
        )
        source_blob = upload_source_blob(
            storage_container,
            ctx.owner_id,
            ctx.design_id,
            revision_number,
            Path(file_path).name,
            code,
        )
        exports = [
            upload_export_blob(
                storage_container,
                ctx.owner_id,
                ctx.design_id,
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
                    owner_id=ctx.owner_id,
                    design_id=ctx.design_id,
                    revision_number=revision_number,
                    image_path=image_path,
                    view=view,
                    source_export_checksum=exports[0].checksum,
                    camera=None,
                    up=None,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                )
            except Exception as exc:
                preview_errors.append(f"{view}: {exc}")

    design_revision = finalize_revision(tables, design_revision, source_blob, exports)
    preview_error = (
        "\npreview_failed: " + "; ".join(preview_errors) if preview_errors else ""
    )
    content_blocks = [
        create_text_block(
            text=(
                "Created design revision.\n"
                f"design_id: {ctx.design_id}\n"
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
                        "texocad_artifact": revision_artifact_payload(
                            artifact_type="texocad.design_revision",
                            design_id=ctx.design_id,
                            design_revision=design_revision,
                            source_blob=source_blob,
                            exports=exports,
                            previews=[
                                blob
                                for preview in previews.values()
                                for blob in preview["blobs"]
                            ],
                            thread_id=ctx.thread_id,
                            run_id=ctx.run_id,
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
