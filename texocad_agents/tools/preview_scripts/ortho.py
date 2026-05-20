import json
import os
from pathlib import Path

import fitz
from build123d import ColorIndex, ExportSVG, LineType, Unit, import_step

SIZE = 1024
CONFIG_FILE = "preview_config.json"
VIEWS = {
    "iso": ((100, -100, 80), (0, 0, 1)),
    "front": ((0, -100, 0), (0, 0, 1)),
    "back": ((0, 100, 0), (0, 0, 1)),
    "left": ((-100, 0, 0), (0, 0, 1)),
    "right": ((100, 0, 0), (0, 0, 1)),
    "top": ((0, 0, 100), (0, 1, 0)),
    "bottom": ((0, 0, -100), (0, 1, 0)),
}


def main() -> None:
    out_dir = Path(os.environ["TEXOCAD_OUTPUT_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(CONFIG_FILE).read_text())
    step_path = find_step(out_dir, config.get("step_filename", ""))
    image_path = render_step(step_path, out_dir, config)
    print(f"preview: {image_path.name}")


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


def render_step(step_file: Path, out_dir: Path, config: dict) -> Path:
    camera = config.get("camera")
    if camera is not None:
        view_name = "custom"
        origin = tuple(float(v) for v in camera)
        viewport_up = tuple(float(v) for v in (config.get("up") or (0, 0, 1)))
    else:
        view_name = config.get("view", "iso").lower()
        if view_name not in VIEWS:
            allowed = ", ".join(sorted(VIEWS))
            raise ValueError(f"unknown view {view_name!r}; expected one of: {allowed}")
        origin, viewport_up = VIEWS[view_name]

    shape = import_step(step_file)
    visible, hidden = shape.project_to_viewport(
        origin,
        viewport_up,
        look_at=(0, 0, 0),
    )
    if not visible and not hidden:
        raise ValueError(f"{view_name} projection produced no visible or hidden edges")

    svg_path = Path(f"{step_file.stem}_{view_name}_ortho.svg")
    png_path = out_dir / f"{step_file.stem}_{view_name}_ortho.png"

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

    svg_to_png(svg_path, png_path)
    return png_path


def svg_to_png(svg_path: Path, png_path: Path) -> None:
    document = fitz.open(stream=svg_path.read_bytes(), filetype="svg")
    try:
        page = document[0]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        scale = SIZE / max(width, height)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        png_path.write_bytes(pixmap.tobytes("png"))
    finally:
        document.close()


if __name__ == "__main__":
    main()
