import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
from _shared import EPS, SHADED_DIRECTIONS, VIEW_UPS, find_step
from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import (
    TopAbs_FACE,
    TopAbs_REVERSED,
    TopAbs_SHELL,
    TopAbs_SOLID,
)
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_DocumentTool
from PIL import Image, ImageDraw

FOV_DEGREES = 42.0
SIZE = 1024
PADDING = 32
DEFAULT_SIZE = (SIZE, SIZE)
DEFAULT_COLOR = np.array([0xCF, 0xD6, 0xDF], dtype=np.float32) / 255.0
BACKGROUND_COLOR = np.array([255, 255, 255], dtype=np.uint8)
EDGE_COLOR = np.array([0x10, 0x18, 0x20], dtype=np.float32)
KEY_LIGHT = np.array([1.0, -1.0, 2.0], dtype=np.float64)
KEY_LIGHT /= np.linalg.norm(KEY_LIGHT)
FILL_LIGHT = np.array([-2.0, 1.0, 1.0], dtype=np.float64)
FILL_LIGHT /= np.linalg.norm(FILL_LIGHT)
TOP_LIGHT = np.array([0.0, 0.0, 1.0], dtype=np.float64)
CONFIG_FILE = "render_config.json"


@dataclass(frozen=True)
class Triangle:
    points: np.ndarray
    color: np.ndarray


@dataclass(frozen=True)
class EdgeSegment:
    points: np.ndarray


@dataclass(frozen=True)
class RenderGeometry:
    triangles: list[Triangle]
    edges: list[EdgeSegment]


@dataclass(frozen=True)
class Camera:
    position: np.ndarray
    target: np.ndarray
    up: np.ndarray


def main() -> None:
    out_dir = Path(os.environ["TEXOCAD_OUTPUT_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(CONFIG_FILE).read_text())
    step_path = find_step(out_dir, config.get("step_filename", ""))

    geometry = load_step_geometry(step_path)

    if config.get("camera") is not None:
        view_name = "custom"
        camera = custom_camera(
            raw_position=vector_from_sequence(config["camera"]),
            raw_target=vector_from_sequence(config.get("target") or (0.0, 0.0, 0.0)),
            raw_up=vector_from_sequence(config.get("up") or (0.0, 0.0, 1.0)),
            triangles=geometry.triangles,
            size=DEFAULT_SIZE,
        )
    else:
        view_name = config.get("view", "iso").lower()
        if view_name not in SHADED_DIRECTIONS:
            known = ", ".join(sorted(SHADED_DIRECTIONS))
            raise ValueError(f"Unknown view {view_name!r}. Use one of: {known}.")
        camera = preset_camera(view_name, geometry.triangles, DEFAULT_SIZE)

    output_path = out_dir / f"{step_path.stem}_{view_name}_shaded.png"
    render_png(geometry, camera, output_path, DEFAULT_SIZE, draw_edges=True)
    print(f"render: {output_path.name}")


def normalize(vector: np.ndarray) -> np.ndarray:
    length = np.linalg.norm(vector)
    if length <= EPS:
        raise ValueError("Cannot normalize a zero-length vector.")
    return vector / length


def solid_color(solid: object, color_tool: object, shape_tool: object) -> np.ndarray:
    """Look up the XDE color for a solid, falling back to DEFAULT_COLOR."""
    color = Quantity_Color()
    if color_tool.GetColor(solid, XCAFDoc_ColorSurf, color) or color_tool.GetColor(
        solid, XCAFDoc_ColorGen, color
    ):
        return np.array([color.Red(), color.Green(), color.Blue()], dtype=np.float32)

    label = shape_tool.FindShape(solid)
    if label and not label.IsNull():
        ref_shape = shape_tool.GetShape_s(label)
        if color_tool.GetColor(
            ref_shape, XCAFDoc_ColorSurf, color
        ) or color_tool.GetColor(ref_shape, XCAFDoc_ColorGen, color):
            return np.array(
                [color.Red(), color.Green(), color.Blue()], dtype=np.float32
            )

    return DEFAULT_COLOR


def face_geometry(shape: object, color: np.ndarray) -> RenderGeometry:
    triangles: list[Triangle] = []
    edges: list[EdgeSegment] = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)

    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            explorer.Next()
            continue

        transform = location.Transformation()
        nodes = []
        for index in range(1, triangulation.NbNodes() + 1):
            point = triangulation.Node(index).Transformed(transform)
            nodes.append(np.array([point.X(), point.Y(), point.Z()], dtype=np.float64))

        reversed_face = face.Orientation() == TopAbs_REVERSED
        face_triangle_indices = []
        for index in range(1, triangulation.NbTriangles() + 1):
            i1, i2, i3 = triangulation.Triangle(index).Get()
            if reversed_face:
                i2, i3 = i3, i2
            face_triangle_indices.append((i1, i2, i3))
            triangles.append(
                Triangle(np.array([nodes[i1 - 1], nodes[i2 - 1], nodes[i3 - 1]]), color)
            )

        edge_counts: dict[tuple[int, int], int] = {}
        for i1, i2, i3 in face_triangle_indices:
            for first, second in ((i1, i2), (i2, i3), (i3, i1)):
                key = tuple(sorted((first, second)))
                edge_counts[key] = edge_counts.get(key, 0) + 1

        for (first, second), count in edge_counts.items():
            if count == 1:
                edges.append(
                    EdgeSegment(np.array([nodes[first - 1], nodes[second - 1]]))
                )

        explorer.Next()

    return RenderGeometry(triangles=triangles, edges=edges)


def load_step_geometry(step_file: Path) -> RenderGeometry:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)

    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    status = reader.ReadFile(str(step_file))
    if status.name != "IFSelect_RetDone":
        raise ValueError(f"Failed to read STEP file: {step_file}")
    reader.Transfer(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(labels)
    if labels.Length() == 0:
        raise ValueError(f"No shapes found in {step_file}.")

    triangles: list[Triangle] = []
    edges: list[EdgeSegment] = []

    for label_index in range(1, labels.Length() + 1):
        root_shape = shape_tool.GetShape_s(labels.Value(label_index))
        BRepMesh_IncrementalMesh(root_shape, 0.001, True, 0.5, True)
        root_triangles, root_edges = _geometry_from_root(
            root_shape, color_tool, shape_tool
        )
        triangles.extend(root_triangles)
        edges.extend(root_edges)

    if not triangles:
        raise ValueError(f"No renderable faces found in {step_file}.")
    return RenderGeometry(triangles=triangles, edges=edges)


def _geometry_from_root(
    root_shape: object,
    color_tool: object,
    shape_tool: object,
) -> tuple[list[Triangle], list[EdgeSegment]]:
    """Triangulate the best-available bodies under a single root shape.

    Falls back from SOLID → SHELL → FACE so multi-root assemblies, shell-only
    STEPs, and face-only STEPs all render. SOLID gets the XDE color lookup;
    SHELL/FACE roots use DEFAULT_COLOR.
    """
    triangles: list[Triangle] = []
    edges: list[EdgeSegment] = []

    solid_explorer = TopExp_Explorer(root_shape, TopAbs_SOLID)
    while solid_explorer.More():
        solid = TopoDS.Solid_s(solid_explorer.Current())
        color = solid_color(solid, color_tool, shape_tool)
        geometry = face_geometry(solid, color)
        triangles.extend(geometry.triangles)
        edges.extend(geometry.edges)
        solid_explorer.Next()
    if triangles:
        return triangles, edges

    shell_explorer = TopExp_Explorer(root_shape, TopAbs_SHELL)
    while shell_explorer.More():
        shell = shell_explorer.Current()
        geometry = face_geometry(shell, DEFAULT_COLOR)
        triangles.extend(geometry.triangles)
        edges.extend(geometry.edges)
        shell_explorer.Next()
    if triangles:
        return triangles, edges

    geometry = face_geometry(root_shape, DEFAULT_COLOR)
    return geometry.triangles, geometry.edges


def preset_camera(
    view: str, triangles: list[Triangle], size: tuple[int, int]
) -> Camera:
    direction = normalize(np.array(SHADED_DIRECTIONS[view], dtype=np.float64))
    up = np.array(VIEW_UPS[view], dtype=np.float64)
    return _fit_camera(direction=direction, up=up, triangles=triangles, size=size)


def custom_camera(
    *,
    raw_position: np.ndarray,
    raw_target: np.ndarray,
    raw_up: np.ndarray,
    triangles: list[Triangle],
    size: tuple[int, int],
) -> Camera:
    """Use camera/target as a direction hint, then fit to projected bounds."""
    direction_vec = raw_position - raw_target
    if float(np.linalg.norm(direction_vec)) < EPS:
        direction_vec = np.array([1.0, -1.0, 0.8], dtype=np.float64)
    direction = normalize(direction_vec)
    up_vec = (
        raw_up
        if float(np.linalg.norm(raw_up)) > EPS
        else np.array([0.0, 0.0, 1.0], dtype=np.float64)
    )
    return _fit_camera(
        direction=direction, up=normalize(up_vec), triangles=triangles, size=size
    )


def _fit_camera(
    *,
    direction: np.ndarray,
    up: np.ndarray,
    triangles: list[Triangle],
    size: tuple[int, int],
) -> Camera:
    width, height = size
    points = np.vstack([triangle.points for triangle in triangles])
    center = (points.min(axis=0) + points.max(axis=0)) * 0.5
    camera = Camera(position=center + direction, target=center, up=up)
    right, view_up, forward = camera_basis(camera)
    offsets = points - center
    camera_x = offsets @ right
    camera_y = offsets @ view_up
    target = (
        center
        + right * float((camera_x.min() + camera_x.max()) * 0.5)
        + view_up * float((camera_y.min() + camera_y.max()) * 0.5)
    )

    offsets = points - target
    camera_x = offsets @ right
    camera_y = offsets @ view_up
    camera_z = offsets @ forward

    focal = (height * 0.5) / math.tan(math.radians(FOV_DEGREES) * 0.5)
    max_x = max(width * 0.5 - PADDING, 1.0)
    max_y = max(height * 0.5 - PADDING, 1.0)
    distance = max(
        float(np.max(np.abs(camera_x) * focal / max_x - camera_z)),
        float(np.max(np.abs(camera_y) * focal / max_y - camera_z)),
        float(-np.min(camera_z) + EPS),
        EPS,
    )
    return Camera(
        position=target + direction * distance,
        target=target,
        up=up,
    )


def camera_basis(camera: Camera) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    forward = normalize(camera.target - camera.position)
    view_up = normalize(camera.up)
    right = np.cross(forward, view_up)
    if np.linalg.norm(right) <= EPS:
        view_up = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        right = np.cross(forward, view_up)
        if np.linalg.norm(right) <= EPS:
            raise ValueError("camera and up vectors must not be parallel")
    right = normalize(right)
    up = normalize(np.cross(right, forward))
    return right, up, forward


def project_points(
    points: np.ndarray, camera: Camera, size: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    width, height = size
    right, up, forward = camera_basis(camera)
    relative = points - camera.position
    camera_x = relative @ right
    camera_y = relative @ up
    camera_z = relative @ forward

    focal = (height * 0.5) / math.tan(math.radians(FOV_DEGREES) * 0.5)
    screen_x = width * 0.5 + (camera_x * focal / camera_z)
    screen_y = height * 0.5 - (camera_y * focal / camera_z)
    return np.column_stack((screen_x, screen_y)), camera_z


def shade_color(color: np.ndarray, points: np.ndarray, camera: Camera) -> np.ndarray:
    normal = np.cross(points[1] - points[0], points[2] - points[0])
    if np.linalg.norm(normal) <= EPS:
        return color

    normal = normalize(normal)
    view_direction = normalize(camera.position - points.mean(axis=0))
    if np.dot(normal, view_direction) < 0:
        normal = -normal

    key = max(float(np.dot(normal, KEY_LIGHT)), 0.0)
    fill = max(float(np.dot(normal, FILL_LIGHT)), 0.0)
    top = max(float(np.dot(normal, TOP_LIGHT)), 0.0)
    shade = min(1.35, 0.55 + key * 0.45 + fill * 0.22 + top * 0.12)
    return np.clip(color * shade, 0.0, 1.0)


def rasterize_triangle(
    color_buffer: np.ndarray,
    depth_buffer: np.ndarray,
    screen: np.ndarray,
    depth: np.ndarray,
    color: np.ndarray,
) -> None:
    height, width, _ = color_buffer.shape
    if np.any(depth <= EPS):
        return

    min_x = max(int(math.floor(float(screen[:, 0].min()))), 0)
    max_x = min(int(math.ceil(float(screen[:, 0].max()))), width - 1)
    min_y = max(int(math.floor(float(screen[:, 1].min()))), 0)
    max_y = min(int(math.ceil(float(screen[:, 1].max()))), height - 1)
    if min_x > max_x or min_y > max_y:
        return

    x0, y0 = screen[0]
    x1, y1 = screen[1]
    x2, y2 = screen[2]
    denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(float(denominator)) <= EPS:
        return

    ys, xs = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
    px = xs + 0.5
    py = ys + 0.5
    w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / denominator
    w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / denominator
    w2 = 1.0 - w0 - w1
    mask = (w0 >= 0.0) & (w1 >= 0.0) & (w2 >= 0.0)
    if not np.any(mask):
        return

    pixel_depth = w0 * depth[0] + w1 * depth[1] + w2 * depth[2]
    current_depth = depth_buffer[min_y : max_y + 1, min_x : max_x + 1]
    visible = mask & (pixel_depth < current_depth)
    if not np.any(visible):
        return

    current_depth[visible] = pixel_depth[visible]
    color_buffer[min_y : max_y + 1, min_x : max_x + 1][visible] = (
        color * 255.0
    ).astype(np.uint8)


def blend_visible_edges(
    image: Image.Image,
    depth_buffer: np.ndarray,
    projected_edges: list[tuple[np.ndarray, np.ndarray]],
    alpha: float = 0.28,
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    edge_rgba = tuple(int(channel) for channel in EDGE_COLOR) + (int(255 * alpha),)

    for screen, depth in projected_edges:
        x1, y1 = screen[0]
        x2, y2 = screen[1]
        z1, z2 = depth[0], depth[1]
        if z1 <= EPS or z2 <= EPS:
            continue

        steps = int(max(abs(x2 - x1), abs(y2 - y1)))
        if steps <= 0:
            continue

        visible_points = []
        for step in range(steps + 1):
            amount = step / steps
            x = x1 + (x2 - x1) * amount
            y = y1 + (y2 - y1) * amount
            px, py = int(round(x)), int(round(y))
            if px < 0 or py < 0 or px >= width or py >= height:
                continue
            z = z1 + (z2 - z1) * amount
            if z <= depth_buffer[py, px] + 0.05:
                visible_points.append((x, y))

        if len(visible_points) >= 2:
            draw.line(visible_points, fill=edge_rgba, width=1)

    image.alpha_composite(overlay)


def render_png(
    geometry: RenderGeometry,
    camera: Camera,
    output_file: Path,
    size: tuple[int, int],
    draw_edges: bool,
) -> None:
    width, height = size
    color_buffer = np.empty((height, width, 3), dtype=np.uint8)
    color_buffer[:, :] = BACKGROUND_COLOR
    depth_buffer = np.full((height, width), np.inf, dtype=np.float64)

    for triangle in geometry.triangles:
        screen, depth = project_points(triangle.points, camera, size)
        shaded = shade_color(triangle.color, triangle.points, camera)
        rasterize_triangle(color_buffer, depth_buffer, screen, depth, shaded)

    image = Image.fromarray(color_buffer, mode="RGB").convert("RGBA")
    if draw_edges:
        projected_edges = [
            project_points(edge.points, camera, size) for edge in geometry.edges
        ]
        blend_visible_edges(image, depth_buffer, projected_edges)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output_file)


def vector_from_sequence(value: Sequence[float] | np.ndarray) -> np.ndarray:
    vector = np.array(value, dtype=np.float64)
    if vector.shape != (3,):
        raise ValueError("Camera vectors must contain exactly three coordinates.")
    return vector


if __name__ == "__main__":
    main()
