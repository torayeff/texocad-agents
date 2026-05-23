import tempfile
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.sandbox import run_python
from texocad_agents.storage import (
    MAX_MODEL_BYTES,
    STEP_CONTENT_TYPE,
    StoredBlob,
    escape_key,
    revision_blob_name,
    save_design_revision_blobs,
    upload_blob,
)
from texocad_agents.tools._runtime import (
    ToolContext,
    create_revision,
    error_message,
    read_runtime_file,
    revision_artifact,
    get_tool_context,
)
from texocad_agents.tools.render_view import (
    ORTHO_STATUS_FILENAME,
    SHADED_STATUS_FILENAME,
    build_render_command,
    save_render,
)

MODEL_EXTENSION = ".step"
SOURCE_CONTENT_TYPE = "text/x-python; charset=utf-8"
ALLOWED_OUTPUT_SUFFIXES = {".step", ".png", ".status"}
RENDER_RUNNER_BY_VIEW: dict[str, tuple[str, str, str]] = {
    "iso_ortho": (ORTHO_STATUS_FILENAME, "ortho.py", "_iso_ortho.png"),
    "iso_shaded": (SHADED_STATUS_FILENAME, "shaded.py", "_iso_shaded.png"),
}


class _SandboxOutputError(Exception):
    pass


@tool(parse_docstring=True)
def create_cad(
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
        return error_message(
            runtime,
            f"Could not find {file_path!r} in the agent filesystem.",
        )

    ctx = get_tool_context(runtime)
    if ctx is None:
        return error_message(
            runtime,
            "Refused: cannot create design artifacts without an authenticated owner and thread.",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_python(
            code,
            Path(tmpdir),
            post_run=build_render_command(),
        )

        if result.exit_code != 0:
            return _sandbox_failure_command(
                runtime,
                design_id=ctx.design_id,
                exit_code=result.exit_code,
                output=result.output,
            )

        try:
            step_files = _validated_step_files(result.files)
        except _SandboxOutputError as exc:
            return _sandbox_failure_command(
                runtime,
                design_id=ctx.design_id,
                exit_code=1,
                output=str(exc),
            )

        tables, storage_container, design_revision, revision_number = create_revision(
            ctx, title=ctx.metadata.get("title") or Path(file_path).stem
        )
        source_blob = _upload_source(
            storage_container,
            ctx,
            revision_number,
            filename=Path(file_path).name,
            code=code,
        )
        exports = [
            _upload_export(storage_container, ctx, revision_number, step_file)
            for step_file in step_files
        ]
        renders, render_errors = _save_renders(
            tables,
            storage_container,
            ctx,
            revision_number=revision_number,
            files=result.files,
            export_checksum=exports[0].checksum,
        )

    design_revision = save_design_revision_blobs(
        tables.design_revisions, design_revision, source_blob, exports
    )
    return _revision_message(
        runtime,
        ctx=ctx,
        design_revision=design_revision,
        source_blob=source_blob,
        exports=exports,
        renders=renders,
        render_errors=render_errors,
    )


def _validated_step_files(files: tuple[Path, ...]) -> tuple[Path, ...]:
    unsupported = [
        file_path.name
        for file_path in files
        if file_path.suffix.lower() not in ALLOWED_OUTPUT_SUFFIXES
    ]
    if unsupported:
        raise _SandboxOutputError(
            "Sandbox output must contain only .step / .png / .status files; "
            f"found unsupported file(s): {', '.join(unsupported)}."
        )

    step_files = tuple(
        file_path for file_path in files if file_path.suffix.lower() == MODEL_EXTENSION
    )
    if len(step_files) != 1:
        raise _SandboxOutputError(
            f"Sandbox must produce exactly 1 .step file; found {len(step_files)}."
        )

    step_file = step_files[0]
    size = step_file.stat().st_size
    if size > MAX_MODEL_BYTES:
        raise _SandboxOutputError(
            f"Sandbox produced oversized model file {step_file.name!r}: "
            f"{size} bytes; limit is {MAX_MODEL_BYTES} bytes."
        )

    return step_files


def _save_renders(
    tables,
    storage_container,
    ctx: ToolContext,
    *,
    revision_number: int,
    files: tuple[Path, ...],
    export_checksum: str,
) -> tuple[dict[str, dict], list[str]]:
    render_outputs = _parse_render_output(files)
    renders: dict[str, dict] = {}
    errors: list[str] = []

    for view, (status, image_path) in render_outputs.items():
        runner_name = RENDER_RUNNER_BY_VIEW[view][1]
        if image_path is None:
            if status is None:
                errors.append(
                    f"{view} render unavailable: {runner_name} produced no PNG"
                )
            elif status == 0:
                errors.append(
                    f"{view} render unavailable: {runner_name} exited 0 "
                    "but produced no PNG"
                )
            else:
                errors.append(f"{view} render unavailable: {runner_name} exit {status}")
            continue
        try:
            renders[view] = save_render(
                tables.design_previews,
                storage_container,
                owner_id=ctx.owner_id,
                design_id=ctx.design_id,
                revision_number=revision_number,
                image_path=image_path,
                view=view,
                source_export_checksum=export_checksum,
                camera=None,
                up=None,
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
            )
        except Exception as exc:
            errors.append(f"{view} render upload failed: {exc}")
    return renders, errors


def _parse_render_output(
    files: tuple[Path, ...],
) -> dict[str, tuple[int | None, Path | None]]:
    by_name = {path.name: path for path in files}
    png_files = [f for f in files if f.suffix.lower() == ".png"]
    outputs: dict[str, tuple[int | None, Path | None]] = {}

    for view, (status_filename, _, png_suffix) in RENDER_RUNNER_BY_VIEW.items():
        path = by_name.get(status_filename)
        if path is None:
            status = None
        else:
            try:
                status = int(path.read_text().strip())
            except ValueError:
                status = None
        matches = [f for f in png_files if f.name.endswith(png_suffix)]
        outputs[view] = (status, matches[0] if len(matches) == 1 else None)

    return outputs


def _upload_source(
    storage_container,
    ctx: ToolContext,
    revision_number: int,
    *,
    filename: str,
    code: str,
) -> StoredBlob:
    blob_name = revision_blob_name(
        ctx.owner_id, ctx.design_id, revision_number, "source", filename
    )
    return upload_blob(
        storage_container,
        blob_name,
        code.encode("utf-8"),
        filename=filename,
        content_type=SOURCE_CONTENT_TYPE,
    )


def _upload_export(
    storage_container,
    ctx: ToolContext,
    revision_number: int,
    model_path: Path,
) -> StoredBlob:
    filename = f"{escape_key(model_path.stem)}{MODEL_EXTENSION}"
    blob_name = revision_blob_name(
        ctx.owner_id,
        ctx.design_id,
        revision_number,
        "exports",
        filename,
    )
    return upload_blob(
        storage_container,
        blob_name,
        model_path,
        filename=filename,
        content_type=STEP_CONTENT_TYPE,
        content_disposition=f'attachment; filename="{filename}"',
    )


def _revision_message(
    runtime: ToolRuntime,
    *,
    ctx: ToolContext,
    design_revision: dict,
    source_blob: StoredBlob,
    exports: list[StoredBlob],
    renders: dict[str, dict],
    render_errors: list[str],
) -> Command:
    render_error_text = (
        "\nrender_failed: " + "; ".join(render_errors) if render_errors else ""
    )
    content_blocks = [
        create_text_block(
            text=(
                "Created design revision.\n"
                f"design_id: {ctx.design_id}\n"
                f"revision_number: {design_revision['revision_number']}"
                f"{render_error_text}"
            )
        )
    ]
    for view, render in renders.items():
        if render.get("image"):
            content_blocks.extend([
                create_text_block(text=f"{view} render"),
                create_image_block(
                    base64=render["image"],
                    mime_type="image/png",
                ),
            ])

    render_blobs = [blob for render in renders.values() for blob in render["blobs"]]
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content_blocks,
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": revision_artifact(
                            artifact_type="texocad.design_revision",
                            design_id=ctx.design_id,
                            design_revision=design_revision,
                            source_blob=source_blob,
                            exports=exports,
                            renders=render_blobs,
                            thread_id=ctx.thread_id,
                            run_id=ctx.run_id,
                        )
                    },
                )
            ]
        }
    )


def _sandbox_failure_command(
    runtime: ToolRuntime,
    *,
    design_id: str,
    exit_code: int,
    output: str,
) -> Command:
    return error_message(
        runtime,
        "Sandbox execution failed.\n"
        f"design_id: {design_id}\n"
        "revision_created: false\n"
        f"exit_code: {exit_code}\n"
        f"output:\n{output.strip()}",
    )
