import math
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import fitz
from build123d import ColorIndex, ExportSVG, LineType, Unit, import_step
from daytona import (
    CreateSandboxFromSnapshotParams,
    CreateSnapshotParams,
    Daytona,
    Image,
    Resources,
)

MODEL_EXTENSION = ".step"
MAX_PROCESS_OUTPUT_CHARS = 20_000
MAX_MODEL_FILES = 1
MAX_MODEL_BYTES = 100 * 1024 * 1024
PREVIEW_IMAGE_SIZE = 1024
PREVIEW_VIEWS = {
    "iso": ((100, -100, 80), (0, 0, 1)),
    "front": ((0, -100, 0), (0, 0, 1)),
    "back": ((0, 100, 0), (0, 0, 1)),
    "left": ((-100, 0, 0), (0, 0, 1)),
    "right": ((100, 0, 0), (0, 0, 1)),
    "top": ((0, 0, 100), (0, 1, 0)),
    "bottom": ((0, 0, -100), (0, 1, 0)),
}


@dataclass(frozen=True)
class SandboxConfig:
    snapshot_name: str = "texocad-build123d"
    python_version: str = "3.12"
    pip_packages: tuple[str, ...] = ("build123d", "bd_warehouse")
    apt_packages: tuple[str, ...] = ("libgl1", "libxrender1")
    cpu: int = 4
    memory: int = 8
    create_timeout: int = 300
    exec_timeout: int = 300
    snapshot_poll_interval: float = 2.0
    network_block_all: bool = True


SANDBOX_CONFIG = SandboxConfig()


@dataclass(frozen=True)
class SandboxRunResult:
    exit_code: int
    output: str
    model_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SandboxPreviewResult:
    exit_code: int
    output: str
    model_path: Path | None = None
    image_path: Path | None = None


def ensure_snapshot(daytona: Daytona, config: SandboxConfig = SANDBOX_CONFIG) -> None:
    try:
        snapshot = daytona.snapshot.get(config.snapshot_name)
    except Exception as exc:
        if "not found" not in str(exc).lower():
            raise

        apt_packages = " ".join(config.apt_packages)
        image = (
            Image
            .debian_slim(config.python_version)
            .run_commands(
                "apt-get update && "
                f"apt-get install -y --no-install-recommends {apt_packages} && "
                "rm -rf /var/lib/apt/lists/*"
            )
            .pip_install(*config.pip_packages)
        )
        snapshot = daytona.snapshot.create(
            CreateSnapshotParams(
                name=config.snapshot_name,
                image=image,
                resources=Resources(cpu=config.cpu, memory=config.memory),
            ),
            timeout=config.create_timeout,
        )

    state = _snapshot_state(snapshot)
    if state == "inactive":
        snapshot = daytona.snapshot.activate(snapshot)

    _wait_for_active_snapshot(daytona, snapshot, config)


def run_code(
    daytona: Daytona,
    code: str,
    output_dir: Path,
    config: SandboxConfig = SANDBOX_CONFIG,
    *,
    support_files: dict[str, bytes] | None = None,
) -> SandboxRunResult:
    sandbox = None

    try:
        sandbox = daytona.create(
            CreateSandboxFromSnapshotParams(
                name=f"texocad-run-{uuid4().hex}",
                snapshot=config.snapshot_name,
                language="python",
                ephemeral=True,
                network_block_all=config.network_block_all,
            ),
            timeout=config.create_timeout,
        )

        work_dir = f"/tmp/texocad-{uuid4().hex}"
        remote_output_dir = posixpath.join(work_dir, "outputs")
        sandbox.fs.create_folder(work_dir, "755")
        sandbox.fs.create_folder(remote_output_dir, "755")
        sandbox.fs.upload_file(
            code.encode("utf-8"), posixpath.join(work_dir, "main.py")
        )
        for filename, contents in sorted((support_files or {}).items()):
            sandbox.fs.upload_file(
                contents,
                posixpath.join(work_dir, posixpath.basename(filename)),
            )

        response = sandbox.process.exec(
            "python main.py 2>&1",
            cwd=work_dir,
            env={"TEXOCAD_OUTPUT_DIR": remote_output_dir},
            timeout=config.exec_timeout,
        )
        raw_output = response.result or ""
        output = raw_output
        if len(raw_output) > MAX_PROCESS_OUTPUT_CHARS:
            output = (
                raw_output[:MAX_PROCESS_OUTPUT_CHARS]
                + "\n...[sandbox output truncated]..."
            )
        if response.exit_code != 0:
            return SandboxRunResult(exit_code=response.exit_code, output=output)

        remote_output_files = sorted(
            (
                posixpath.join(remote_output_dir, item.name),
                int(getattr(item, "size", 0) or 0),
            )
            for item in sandbox.fs.list_files(remote_output_dir)
            if not item.is_dir
        )
        output_validation_error = _validate_output_files(remote_output_files)
        if output_validation_error:
            return output_validation_error

        remote_model_files = [
            file_info
            for file_info in remote_output_files
            if posixpath.splitext(file_info[0])[1].lower() == MODEL_EXTENSION
        ]
        remote_validation_error = _validate_remote_model_files(remote_model_files)
        if remote_validation_error:
            return remote_validation_error

        output_dir.mkdir(parents=True, exist_ok=True)
        remote_model_path, _ = remote_model_files[0]
        model_bytes = sandbox.fs.download_file(remote_model_path)
        download_validation_error = _validate_downloaded_model_file(
            remote_model_path,
            len(model_bytes),
        )
        if download_validation_error:
            return download_validation_error

        local_model_path = output_dir / posixpath.basename(remote_model_path)
        local_model_path.write_bytes(model_bytes)

        return SandboxRunResult(
            exit_code=0,
            output=output,
            model_paths=(local_model_path,),
        )
    finally:
        if sandbox is not None:
            try:
                sandbox.delete(timeout=60)
            except Exception:
                pass


def run_code_preview(
    daytona: Daytona,
    code: str,
    output_dir: Path,
    config: SandboxConfig = SANDBOX_CONFIG,
    *,
    view: str = "iso",
    camera: tuple[float, float, float] | None = None,
    up: tuple[float, float, float] | None = None,
    support_files: dict[str, bytes] | None = None,
) -> SandboxPreviewResult:
    result = run_code(
        daytona,
        code,
        output_dir,
        config,
        support_files=support_files,
    )
    if result.exit_code != 0:
        return SandboxPreviewResult(exit_code=result.exit_code, output=result.output)
    if len(result.model_paths) != 1:
        return SandboxPreviewResult(
            exit_code=1,
            output=f"Expected exactly one STEP export, found {len(result.model_paths)}.",
        )

    model_path = result.model_paths[0]
    try:
        image_path = render_step_preview(
            model_path,
            output_dir,
            view=view,
            camera=camera,
            up=up,
        )
    except Exception as exc:
        return SandboxPreviewResult(
            exit_code=1,
            output=f"Preview render failed for {model_path.name!r}: {exc}",
            model_path=model_path,
        )

    return SandboxPreviewResult(
        exit_code=0,
        output=result.output,
        model_path=model_path,
        image_path=image_path,
    )


def render_step_preview(
    model_path: Path,
    output_dir: Path,
    *,
    view: str = "iso",
    camera: tuple[float, float, float] | None = None,
    up: tuple[float, float, float] | None = None,
    size: int = PREVIEW_IMAGE_SIZE,
) -> Path:
    if camera is None:
        view_name = view.lower()
        if view_name not in PREVIEW_VIEWS:
            allowed = ", ".join(sorted(PREVIEW_VIEWS))
            raise ValueError(f"unknown view {view!r}; expected one of: {allowed}")
        viewport_origin, viewport_up = PREVIEW_VIEWS[view_name]
    else:
        view_name = "custom"
        viewport_origin = _validate_preview_vector(camera, "camera")
        viewport_up = _validate_preview_vector(up or (0, 0, 1), "up")
        _validate_preview_axes(viewport_origin, viewport_up)

    shape = import_step(model_path)
    visible, hidden = shape.project_to_viewport(
        viewport_origin,
        viewport_up,
        look_at=(0, 0, 0),
    )
    if not visible and not hidden:
        raise ValueError(f"{view_name} projection produced no visible or hidden edges")

    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / f"{model_path.stem}_{view_name}.svg"
    png_path = output_dir / f"{model_path.stem}_{view_name}.png"

    exporter = ExportSVG(unit=Unit.MM, margin=10, fit_to_stroke=True)
    exporter.add_layer("Visible", line_color=ColorIndex.BLACK, line_weight=0.35)
    exporter.add_layer(
        "Hidden",
        line_color=ColorIndex.GRAY,
        line_weight=0.18,
        line_type=LineType.ISO_DOT,
    )
    if hidden:
        exporter.add_shape(hidden, layer="Hidden")
    if visible:
        exporter.add_shape(visible, layer="Visible")
    exporter.write(svg_path)

    _convert_svg_to_png(svg_path, png_path, size)
    return png_path


def _validate_preview_vector(
    vector: tuple[float, float, float],
    name: str,
) -> tuple[float, float, float]:
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError(f"{name} must be a 3-number vector")

    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{name} must contain finite numbers")
    if _vector_length(values) == 0:
        raise ValueError(f"{name} must not be the zero vector")

    return values


def _validate_preview_axes(
    camera: tuple[float, float, float],
    up: tuple[float, float, float],
) -> None:
    cross = (
        camera[1] * up[2] - camera[2] * up[1],
        camera[2] * up[0] - camera[0] * up[2],
        camera[0] * up[1] - camera[1] * up[0],
    )
    if _vector_length(cross) <= 1e-9:
        raise ValueError("camera and up vectors must not be parallel")


def _vector_length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _convert_svg_to_png(svg_path: Path, png_path: Path, size: int) -> None:
    document = fitz.open(stream=svg_path.read_bytes(), filetype="svg")
    try:
        page = document[0]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        scale = size / max(width, height)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        png_path.write_bytes(pixmap.tobytes("png"))
    finally:
        document.close()


def _validate_output_files(
    remote_output_files: list[tuple[str, int]],
) -> SandboxRunResult | None:
    unsupported_files = [
        posixpath.basename(remote_path)
        for remote_path, _ in remote_output_files
        if posixpath.splitext(remote_path)[1].lower() != MODEL_EXTENSION
    ]
    if not unsupported_files:
        return None

    return SandboxRunResult(
        exit_code=1,
        output=(
            "Sandbox output must contain only .step files; found unsupported "
            f"file(s): {', '.join(unsupported_files)}."
        ),
    )


def _validate_remote_model_files(
    remote_model_files: list[tuple[str, int]],
) -> SandboxRunResult | None:
    if len(remote_model_files) != MAX_MODEL_FILES:
        return SandboxRunResult(
            exit_code=1,
            output=(
                f"Sandbox must produce exactly {MAX_MODEL_FILES} .step file; "
                f"found {len(remote_model_files)}."
            ),
        )

    for remote_model_path, size in remote_model_files:
        if size > MAX_MODEL_BYTES:
            return SandboxRunResult(
                exit_code=1,
                output=(
                    f"Sandbox produced oversized model file "
                    f"{posixpath.basename(remote_model_path)!r}: {size} bytes; "
                    f"limit is {MAX_MODEL_BYTES} bytes."
                ),
            )

    return None


def _validate_downloaded_model_file(
    remote_model_path: str,
    model_size: int,
) -> SandboxRunResult | None:
    if model_size > MAX_MODEL_BYTES:
        return SandboxRunResult(
            exit_code=1,
            output=(
                f"Sandbox produced oversized model file "
                f"{posixpath.basename(remote_model_path)!r}: {model_size} bytes; "
                f"limit is {MAX_MODEL_BYTES} bytes."
            ),
        )

    return None


def _snapshot_state(snapshot: object) -> str:
    raw_state = getattr(snapshot, "state", "")
    state_value = getattr(raw_state, "value", raw_state)
    return str(state_value).strip().lower().split(".")[-1].replace("_", " ")


def _wait_for_active_snapshot(
    daytona: Daytona,
    snapshot: object,
    config: SandboxConfig,
) -> None:
    deadline = (
        None if config.create_timeout == 0 else time.monotonic() + config.create_timeout
    )

    while True:
        state = _snapshot_state(snapshot)
        if state == "active":
            return
        if state in {"error", "build failed", "removing"}:
            raise RuntimeError(
                f"Snapshot {config.snapshot_name!r} is not usable; current state: {state}"
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise RuntimeError(
                f"Snapshot {config.snapshot_name!r} did not become active before timeout; "
                f"current state: {state or 'unknown'}"
            )

        time.sleep(config.snapshot_poll_interval)
        snapshot = daytona.snapshot.get(config.snapshot_name)
