import posixpath
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

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
