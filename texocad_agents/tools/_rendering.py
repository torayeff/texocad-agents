import base64
import json
from pathlib import Path

from texocad_agents.sandbox import SandboxCommand, run_python
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
    return json.dumps({
        "step_filename": step_filename,
        "view": view,
        "camera": camera,
        "up": up,
    }).encode("utf-8")


def _runner_source(name: str) -> str:
    return (RENDER_RUNNERS_DIR / f"{name}.py").read_text()
