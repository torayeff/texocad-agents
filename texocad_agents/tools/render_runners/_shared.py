from pathlib import Path

EPS = 1e-5

ORTHO_PROJECTIONS: dict[str, tuple[float, float, float]] = {
    "iso": (100.0, -100.0, 80.0),
    "front": (0.0, -100.0, 0.0),
    "back": (0.0, 100.0, 0.0),
    "left": (-100.0, 0.0, 0.0),
    "right": (100.0, 0.0, 0.0),
    "top": (0.0, 0.0, 100.0),
    "bottom": (0.0, 0.0, -100.0),
}

SHADED_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "iso": (1.0, -1.0, 0.8),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "top": (0.0, -EPS, 1.0),
    "bottom": (0.0, -EPS, -1.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
}

VIEW_UPS: dict[str, tuple[float, float, float]] = {
    "iso": (0.0, 0.0, 1.0),
    "front": (0.0, 0.0, 1.0),
    "back": (0.0, 0.0, 1.0),
    "top": (0.0, 1.0, 0.0),
    "bottom": (0.0, -1.0, 0.0),
    "left": (0.0, 0.0, 1.0),
    "right": (0.0, 0.0, 1.0),
}


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
