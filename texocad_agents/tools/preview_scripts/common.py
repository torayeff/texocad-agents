from pathlib import Path


def find_step(out_dir: Path, step_filename: str) -> Path:
    if step_filename:
        step_path = out_dir / step_filename
        if step_path.exists():
            return step_path
        return Path(step_filename)

    step_files = list(out_dir.glob("*.step"))
    if len(step_files) != 1:
        raise ValueError(f"expected exactly one STEP file, found {len(step_files)}")
    return step_files[0]
