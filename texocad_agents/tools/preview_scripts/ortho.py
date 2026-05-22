import json
import math
import os
from pathlib import Path

import cadquery as cq
import fitz
import numpy as np
from common import find_step
from PIL import Image, ImageChops

SIZE = 1024
PADDING = 32
SVG_MARGIN = 16
RENDER_OVERSAMPLE = 2
CONFIG_FILE = "preview_config.json"
VIEWS: dict[str, tuple[float, float, float]] = {
    "iso": (100, -100, 80),
    "front": (0, -100, 0),
    "back": (0, 100, 0),
    "left": (-100, 0, 0),
    "right": (100, 0, 0),
    "top": (0, 0, 100),
    "bottom": (0, 0, -100),
}
DEFAULT_UPS: dict[str, tuple[float, float, float]] = {
    "iso": (0.0, 0.0, 1.0),
    "front": (0.0, 0.0, 1.0),
    "back": (0.0, 0.0, 1.0),
    "left": (0.0, 0.0, 1.0),
    "right": (0.0, 0.0, 1.0),
    "top": (0.0, 1.0, 0.0),
    "bottom": (0.0, -1.0, 0.0),
}


def main() -> None:
    out_dir = Path(os.environ["TEXOCAD_OUTPUT_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(CONFIG_FILE).read_text())
    step_path = find_step(out_dir, config.get("step_filename", ""))
    image_path = render_step(step_path, out_dir, config)
    print(f"preview: {image_path.name}")


def render_step(step_file: Path, out_dir: Path, config: dict) -> Path:
    camera = config.get("camera")
    up = config.get("up")
    if camera is not None:
        view_name = "custom"
        projection = tuple(float(v) for v in camera)
        up_vec = tuple(float(v) for v in up) if up else (0.0, 0.0, 1.0)
    else:
        view_name = config.get("view", "iso").lower()
        if view_name not in VIEWS:
            allowed = ", ".join(sorted(VIEWS))
            raise ValueError(f"unknown view {view_name!r}; expected one of: {allowed}")
        projection = VIEWS[view_name]
        up_vec = DEFAULT_UPS[view_name]

    shape = cq.importers.importStep(str(step_file))

    svg_path = Path(f"{step_file.stem}_{view_name}_ortho.svg")
    png_path = out_dir / f"{step_file.stem}_{view_name}_ortho.png"

    cq.exporters.export(
        shape,
        str(svg_path),
        opt={
            "width": SIZE + 2 * SVG_MARGIN,
            "height": SIZE + 2 * SVG_MARGIN,
            "marginLeft": SVG_MARGIN,
            "marginTop": SVG_MARGIN,
            "showAxes": False,
            "showHidden": True,
            "projectionDir": projection,
            "strokeColor": (0, 0, 0),
            "hiddenColor": (140, 140, 140),
            "strokeWidth": 0.35,
        },
    )

    rotation_deg = compute_rotation_degrees(projection, up_vec)
    svg_to_png(svg_path, png_path, rotation_deg)
    return png_path


def svg_to_png(svg_path: Path, png_path: Path, rotation_deg: float) -> None:
    document = fitz.open(stream=svg_path.read_bytes(), filetype="svg")
    try:
        page = document[0]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        scale = (SIZE * RENDER_OVERSAMPLE) / max(width, height)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        raw = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        if abs(rotation_deg) > 0.05:
            raw = raw.rotate(
                rotation_deg,
                resample=Image.BICUBIC,
                fillcolor=(255, 255, 255),
                expand=True,
            )
        trim_and_center(raw, SIZE, PADDING).save(png_path, format="PNG")
    finally:
        document.close()


def trim_and_center(image: Image.Image, size: int, padding: int) -> Image.Image:
    white = Image.new(image.mode, image.size, (255, 255, 255))
    bbox = ImageChops.difference(image, white).getbbox()
    if bbox is None:
        return Image.new("RGB", (size, size), (255, 255, 255))

    content = image.crop(bbox)
    target = max(size - 2 * padding, 1)
    aspect = content.width / max(content.height, 1)
    if aspect >= 1.0:
        new_width = target
        new_height = max(int(round(target / aspect)), 1)
    else:
        new_height = target
        new_width = max(int(round(target * aspect)), 1)
    resized = content.resize((new_width, new_height), Image.LANCZOS)

    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    canvas.paste(resized, ((size - new_width) // 2, (size - new_height) // 2))
    return canvas


def compute_rotation_degrees(
    projection_dir: tuple[float, float, float],
    up: tuple[float, float, float],
) -> float:
    """Angle (degrees, CCW) to rotate the rasterized SVG so `up` lands at image-up.

    cadquery's getSVG uses gp_Ax2(point, projectionDir) with no X-axis control,
    so OCP picks the in-plane X axis from a deterministic heuristic that ignores
    `up`. We measure the signed angle between cadquery's chosen plane-X and the
    desired right vector (up x projection_dir) and rotate by its negative.
    """
    N = np.array(projection_dir, dtype=float)
    norm = float(np.linalg.norm(N))
    if norm < 1e-9:
        return 0.0
    N = N / norm

    up_arr = np.array(up, dtype=float)
    up_proj = up_arr - float(np.dot(up_arr, N)) * N
    if float(np.linalg.norm(up_proj)) < 1e-6:
        fallback = (
            np.array([0.0, 0.0, 1.0]) if abs(N[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
        )
        up_proj = fallback - float(np.dot(fallback, N)) * N
    up_proj = up_proj / float(np.linalg.norm(up_proj))

    desired_right = np.cross(up_proj, N)
    cq_x = _cadquery_plane_x(N)

    cos_t = float(np.dot(desired_right, cq_x))
    sin_t = float(np.dot(N, np.cross(cq_x, desired_right)))
    return -math.degrees(math.atan2(sin_t, cos_t))


def _cadquery_plane_x(normal: np.ndarray) -> np.ndarray:
    """In-plane X axis cadquery's getSVG would pick for this projection normal.

    Ports OpenCascade's gp_Ax2(P, V) auto-selection: choose an auxiliary
    direction D from the smallest-magnitude component of N, then derive
    plane X as N x (D x N).
    """
    a, b, c = float(normal[0]), float(normal[1]), float(normal[2])
    a_abs, b_abs, c_abs = abs(a), abs(b), abs(c)
    if b_abs <= a_abs and b_abs <= c_abs:
        d = np.array([-c, 0.0, a]) if a_abs > c_abs else np.array([c, 0.0, -a])
    elif a_abs <= b_abs and a_abs <= c_abs:
        d = np.array([0.0, -c, b]) if b_abs > c_abs else np.array([0.0, c, -b])
    else:
        d = np.array([-b, a, 0.0]) if a_abs > b_abs else np.array([b, -a, 0.0])

    plane_x = np.cross(normal, np.cross(d, normal))
    n = float(np.linalg.norm(plane_x))
    if n < 1e-9:
        return np.array([1.0, 0.0, 0.0])
    return plane_x / n


if __name__ == "__main__":
    main()
