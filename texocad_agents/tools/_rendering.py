"""Shared render helpers for CAD revision previews."""

import base64
import json
from pathlib import Path

from texocad_agents.sandbox.daytona.daytona import SandboxCommand, run_python
from texocad_agents.storage import (
    RENDER_CONTENT_TYPE,
    escape_key,
    revision_blob_name,
    save_design_preview,
    upload_blob,
)

RENDER_RUNNERS_DIR = Path(__file__).with_name("render_runners")
RENDER_CONFIG_FILE = "render_config.json"
SHARED_RUNNER_MODULE = "_shared.py"
DEFAULT_RENDER_STYLES = ("ortho", "shaded")


def save_render(
    design_previews_table,
    storage_container,
    *,
    owner_id: str,
    design_id: str,
    revision_number: int,
    image_path: Path | None,
    view: str,
    source_export_checksum: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
    thread_id: str,
    run_id: str | None,
) -> dict:
    """Upload a render image and persist preview metadata.

    Args:
        design_previews_table: Azure Table client for preview rows.
        storage_container: Azure Blob container client.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        revision_number: Revision number the render belongs to.
        image_path: Local PNG path produced by a render runner.
        view: Stored preview view name.
        source_export_checksum: Checksum of the STEP export rendered.
        camera: Optional custom camera vector.
        up: Optional custom up vector.
        thread_id: LangGraph thread that created the preview.
        run_id: Optional LangGraph run that created the preview.

    Returns:
        Dictionary containing stored render blobs and base64 PNG image data.

    Raises:
        ValueError: If `image_path` is None.
    """

    if image_path is None:
        raise ValueError(f"render PNG for view {view!r} was not produced")

    filename = f"{escape_key(view)}.png"
    blob_name = revision_blob_name(
        owner_id, design_id, revision_number, "previews", filename
    )
    render = upload_blob(
        storage_container,
        blob_name,
        image_path,
        filename=filename,
        content_type=RENDER_CONTENT_TYPE,
        content_disposition=f'inline; filename="{filename}"',
    )
    save_design_preview(
        design_previews_table,
        owner_id,
        design_id,
        revision_number,
        view,
        render,
        source_export_checksum=source_export_checksum,
        camera=camera,
        up=up,
        created_by_thread_id=thread_id,
        created_by_run_id=run_id,
    )
    image = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return {"blobs": [render], "image": image}


def render_step(
    step_bytes: bytes,
    step_filename: str,
    out_dir: Path,
    *,
    view: str = "iso",
    style: str = "ortho",
    camera: tuple[float, float, float] | None = None,
    up: tuple[float, float, float] | None = None,
) -> Path:
    """Render STEP bytes through a sandboxed render runner.

    Args:
        step_bytes: STEP file contents to render.
        step_filename: Filename to use inside the sandbox.
        out_dir: Local directory for downloaded render output.
        view: Named view for preset renders.
        style: Render runner style, such as `ortho` or `shaded`.
        camera: Optional custom camera position vector.
        up: Optional custom up vector.

    Returns:
        Local path to the single rendered PNG.

    Raises:
        ValueError: If rendering fails or does not produce exactly one PNG.
    """

    result = run_python(
        _runner_source(style),
        out_dir,
        support_files={
            step_filename: step_bytes,
            RENDER_CONFIG_FILE: render_config(
                step_filename=step_filename,
                view=view,
                camera=camera,
                up=up,
            ),
            SHARED_RUNNER_MODULE: _runner_source("_shared").encode("utf-8"),
        },
    )
    if result.exit_code != 0:
        raise ValueError(
            "Render failed.\n"
            f"exit_code: {result.exit_code}\n"
            f"output:\n{result.output.strip()}"
        )

    renders = [path for path in result.files if path.suffix.lower() == ".png"]
    if len(renders) != 1:
        raise ValueError(f"Expected exactly one render PNG, found {len(renders)}.")
    return renders[0]


def build_render_command(view: str = "iso") -> SandboxCommand:
    """Build the sandbox post-run command for default render styles.

    Args:
        view: Named view used by default render runners.

    Returns:
        Sandbox command that runs bundled render scripts after CAD export.
    """

    runner_lines = "".join(
        f'echo "=== {style}.py ==="\n'
        f"python {style}.py 2>&1\n"
        'code="$?"\n'
        f'if [ "$code" -ne 0 ]; then echo "render_failed: {style}.py exit $code"; fi\n'
        for style in DEFAULT_RENDER_STYLES
    )
    script = f'set +e\nout="$TEXOCAD_OUTPUT_DIR"\n{runner_lines}exit 0\n'
    return SandboxCommand(
        filename="ortho.py",
        contents=_runner_source("ortho").encode("utf-8"),
        command=script,
        support_files={
            RENDER_CONFIG_FILE: render_config(
                step_filename="",
                view=view,
                camera=None,
                up=None,
            ),
            **{
                f"{style}.py": _runner_source(style).encode("utf-8")
                for style in DEFAULT_RENDER_STYLES
                if style != "ortho"
            },
            SHARED_RUNNER_MODULE: _runner_source("_shared").encode("utf-8"),
        },
    )


def render_config(
    *,
    step_filename: str,
    view: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
) -> bytes:
    """Serialize render runner configuration to JSON bytes.

    Args:
        step_filename: STEP filename for the runner, or empty to auto-detect.
        view: Named view for preset renders.
        camera: Optional custom camera position vector.
        up: Optional custom up vector.

    Returns:
        UTF-8 encoded JSON configuration bytes.
    """

    return json.dumps({
        "step_filename": step_filename,
        "view": view,
        "camera": camera,
        "up": up,
    }).encode("utf-8")


def _runner_source(name: str) -> str:
    """Read a bundled render runner source file.

    Args:
        name: Render runner stem, without the `.py` suffix.

    Returns:
        Render runner Python source text.
    """

    return (RENDER_RUNNERS_DIR / f"{name}.py").read_text()
