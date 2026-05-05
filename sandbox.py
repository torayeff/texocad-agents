import posixpath
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

MODEL_EXTENSIONS = (".step", ".stp", ".glb", ".gltf", ".stl")


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

    state_value = getattr(getattr(snapshot, "state", ""), "value", snapshot.state)
    state = str(state_value).strip().lower().split(".")[-1].replace("_", " ")
    if state == "inactive":
        snapshot = daytona.snapshot.activate(snapshot)
        state_value = getattr(getattr(snapshot, "state", ""), "value", snapshot.state)
        state = str(state_value).strip().lower().split(".")[-1].replace("_", " ")

    if state != "active":
        raise RuntimeError(
            f"Snapshot {config.snapshot_name!r} is not active; current state: {state or 'unknown'}"
        )


def run_code(
    daytona: Daytona,
    code: str,
    output_dir: Path,
    config: SandboxConfig = SANDBOX_CONFIG,
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

        response = sandbox.process.exec(
            "python main.py 2>&1",
            cwd=work_dir,
            env={"TEXOCAD_OUTPUT_DIR": remote_output_dir},
            timeout=config.exec_timeout,
        )
        output = response.result or ""
        if response.exit_code != 0:
            return SandboxRunResult(exit_code=response.exit_code, output=output)

        remote_model_paths = sorted(
            posixpath.join(remote_output_dir, item.name)
            for item in sandbox.fs.list_files(remote_output_dir)
            if not item.is_dir
            and posixpath.splitext(item.name)[1].lower() in MODEL_EXTENSIONS
        )
        if not remote_model_paths:
            return SandboxRunResult(exit_code=0, output=output)

        output_dir.mkdir(parents=True, exist_ok=True)
        local_model_paths = []
        for remote_model_path in remote_model_paths:
            local_model_path = output_dir / posixpath.basename(remote_model_path)
            local_model_path.write_bytes(sandbox.fs.download_file(remote_model_path))
            local_model_paths.append(local_model_path)

        return SandboxRunResult(
            exit_code=0,
            output=output,
            model_paths=tuple(local_model_paths),
        )
    finally:
        if sandbox is not None:
            try:
                sandbox.delete(timeout=60)
            except Exception:
                pass
