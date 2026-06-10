import logging
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

logger = logging.getLogger(__name__)

MAX_PROCESS_OUTPUT_CHARS = 20_000


@dataclass(frozen=True)
class SandboxConfig:
    """Configuration for sandbox snapshot creation and execution.

    Attributes:
        snapshot_name: Daytona snapshot name used as the Python runtime base.
        python_version: Python version for snapshot creation.
        pip_packages: Python packages installed in the snapshot.
        apt_packages: System packages installed in the snapshot.
        cpu: CPU allocation for snapshot and sandbox resources.
        memory: Memory allocation for snapshot and sandbox resources.
        create_timeout: Timeout in seconds for snapshot and sandbox creation.
        exec_timeout: Timeout in seconds for sandbox process execution.
        snapshot_poll_interval: Seconds between snapshot activation polls.
        network_block_all: Whether to block outbound network access.
    """

    snapshot_name: str = "texocad-cadquery-v1"
    python_version: str = "3.12"
    pip_packages: tuple[str, ...] = (
        "cadquery",
        "numpy",
        "pillow",
        "pymupdf",
    )
    apt_packages: tuple[str, ...] = ("libgl1", "libxrender1")
    cpu: int = 4
    memory: int = 8
    create_timeout: int = 900
    exec_timeout: int = 900
    snapshot_poll_interval: float = 2.0
    network_block_all: bool = True


@dataclass(frozen=True)
class SandboxRunResult:
    """Result from a sandboxed Python execution.

    Attributes:
        exit_code: Exit code from the main script or post-run command.
        output: Combined process output, truncated when needed.
        files: Local paths downloaded from the sandbox output directory.
    """

    exit_code: int
    output: str
    files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SandboxCommand:
    """Secondary command to run after the main sandbox script succeeds.

    Attributes:
        filename: Script filename to upload before executing `command`.
        contents: File contents for `filename`.
        command: Shell command executed in the sandbox working directory.
        support_files: Optional extra files uploaded alongside `filename`.
    """

    filename: str
    contents: bytes
    command: str
    support_files: dict[str, bytes] | None = None


def ensure_snapshot(daytona: Daytona, config: SandboxConfig) -> None:
    """Ensure the configured Daytona snapshot exists and is active.

    Creates the snapshot with the configured Debian image, apt packages, Python
    packages, and resources when it does not already exist.

    Args:
        daytona: Daytona client used to inspect or create snapshots.
        config: Sandbox runtime configuration.

    Raises:
        RuntimeError: If the snapshot enters an unusable state or activation
            does not finish before the configured timeout.
        Exception: Propagates unexpected Daytona client failures.
    """

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


def run_python(
    code: str,
    out_dir: Path,
    config: SandboxConfig | None = None,
    *,
    support_files: dict[str, bytes] | None = None,
    post_run: SandboxCommand | None = None,
) -> SandboxRunResult:
    """Run Python code in an ephemeral sandbox and download produced outputs.

    The main script and optional support files are uploaded into a temporary
    working directory. Files written to `TEXOCAD_OUTPUT_DIR` are downloaded into
    `out_dir` after successful execution. When `post_run` is provided, it runs
    after the main script and may add more output files.

    Args:
        code: Python source for the main script.
        out_dir: Local directory that receives downloaded output files.
        config: Optional sandbox configuration. Defaults to `SandboxConfig()`.
        support_files: Additional files uploaded next to the main script.
        post_run: Optional command and support files run after the main script.

    Returns:
        Sandbox execution result with exit code, process output, and downloaded
        file paths. Non-zero exits return without downloading outputs.
    """

    config = config or SandboxConfig()
    daytona = Daytona()
    ensure_snapshot(daytona, config)
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
        env = {"TEXOCAD_OUTPUT_DIR": remote_output_dir}
        sandbox.fs.create_folder(work_dir, "755")
        sandbox.fs.create_folder(remote_output_dir, "755")
        sandbox.fs.upload_file(
            code.encode("utf-8"), posixpath.join(work_dir, "main.py")
        )
        _upload_files(sandbox, work_dir, support_files)

        exit_code, output = _exec(sandbox, "python main.py 2>&1", work_dir, env, config)
        if exit_code != 0:
            return SandboxRunResult(exit_code=exit_code, output=output)

        if post_run is not None:
            _upload_files(sandbox, work_dir, post_run.support_files)
            sandbox.fs.upload_file(
                post_run.contents,
                posixpath.join(work_dir, posixpath.basename(post_run.filename)),
            )
            exit_code, post_output = _exec(
                sandbox, post_run.command, work_dir, env, config
            )
            output = _truncate(f"{output}\n{post_output}")
            if exit_code != 0:
                return SandboxRunResult(exit_code=exit_code, output=output)

        return SandboxRunResult(
            exit_code=0,
            output=output,
            files=_download_outputs(sandbox, remote_output_dir, out_dir),
        )
    finally:
        if sandbox is not None:
            try:
                sandbox.delete(timeout=60)
            except Exception as exc:
                logger.warning(
                    "Failed to delete sandbox %r: %s",
                    getattr(sandbox, "id", None),
                    exc,
                )


def _exec(
    sandbox: object,
    command: str,
    work_dir: str,
    env: dict[str, str],
    config: SandboxConfig,
) -> tuple[int, str]:
    """Execute a shell command inside the sandbox.

    Args:
        sandbox: Daytona sandbox object.
        command: Shell command to execute.
        work_dir: Remote working directory.
        env: Environment variables for the process.
        config: Sandbox runtime configuration.

    Returns:
        Tuple of `(exit_code, truncated_output)`.
    """

    response = sandbox.process.exec(
        command,
        cwd=work_dir,
        env=env,
        timeout=config.exec_timeout,
    )
    return response.exit_code, _truncate(response.result or "")


def _download_outputs(
    sandbox: object, remote_output_dir: str, out_dir: Path
) -> tuple[Path, ...]:
    """Download sandbox output files into a local directory.

    Args:
        sandbox: Daytona sandbox object.
        remote_output_dir: Remote output directory path.
        out_dir: Local destination directory.

    Returns:
        Local paths for downloaded files, sorted by remote filename.
    """

    remote_output_files = sorted(
        posixpath.join(remote_output_dir, item.name)
        for item in sandbox.fs.list_files(remote_output_dir)
        if not item.is_dir
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for remote_path in remote_output_files:
        file_path = out_dir / posixpath.basename(remote_path)
        file_path.write_bytes(sandbox.fs.download_file(remote_path))
        files.append(file_path)
    return tuple(files)


def _upload_files(
    sandbox: object,
    work_dir: str,
    files: dict[str, bytes] | None,
) -> None:
    """Upload support files into a sandbox working directory.

    Args:
        sandbox: Daytona sandbox object.
        work_dir: Remote working directory.
        files: Mapping of filenames to file contents.
    """

    for filename, contents in sorted((files or {}).items()):
        sandbox.fs.upload_file(
            contents,
            posixpath.join(work_dir, posixpath.basename(filename)),
        )


def _snapshot_state(snapshot: object) -> str:
    """Normalize a Daytona snapshot state value.

    Args:
        snapshot: Daytona snapshot object.

    Returns:
        Lowercase snapshot state string.
    """

    raw_state = getattr(snapshot, "state", "")
    state_value = getattr(raw_state, "value", raw_state)
    return str(state_value).strip().lower().split(".")[-1].replace("_", " ")


def _wait_for_active_snapshot(
    daytona: Daytona,
    snapshot: object,
    config: SandboxConfig,
) -> None:
    """Poll until a snapshot becomes active.

    Args:
        daytona: Daytona client used to refresh snapshot state.
        snapshot: Initial Daytona snapshot object.
        config: Sandbox runtime configuration.

    Raises:
        RuntimeError: If the snapshot enters an unusable state or activation
            times out.
    """

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


def _truncate(text: str) -> str:
    """Limit process output to the configured maximum length.

    Args:
        text: Process output text.

    Returns:
        Original or truncated output text.
    """

    if len(text) <= MAX_PROCESS_OUTPUT_CHARS:
        return text
    return text[:MAX_PROCESS_OUTPUT_CHARS] + "\n...[sandbox output truncated]..."
