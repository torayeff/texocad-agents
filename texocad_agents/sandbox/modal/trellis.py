import io
import time
import uuid
from pathlib import Path

import modal
from build_image import app, image

GENERATE_GPU = "H100"
POSTPROCESS_GPU = "L4"
POSTPROCESS_CPU = 8.0
OUTPUT_DIR = "/outputs"
INTERMEDIATE_DIR = "intermediates"
AABB = [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]]
PBR_ATTR_LAYOUT = {
    "base_color": (0, 3),
    "metallic": (3, 4),
    "roughness": (4, 5),
    "alpha": (5, 6),
}

output_volume = modal.Volume.from_name("trellis2-outputs", create_if_missing=True)


@app.cls(
    image=image,
    gpu=GENERATE_GPU,
    timeout=60 * 60,
    scaledown_window=600,
    volumes={OUTPUT_DIR: output_volume},
)
class Trellis2Generate:
    """Stage 1: image -> simplified mesh + PBR attribute volume (GPU-heavy).

    Snapshots are intentionally disabled: serializing the GPU-resident
    pipeline took longer than the plain cold-boot model load it replaces.
    """

    @modal.enter()
    def load(self) -> None:
        from trellis2.pipelines import (  # type: ignore[import-not-found]
            Trellis2ImageTo3DPipeline,
        )

        start = time.monotonic()
        print("Loading TRELLIS.2 pipeline...", flush=True)
        self.pipeline = Trellis2ImageTo3DPipeline.from_pretrained(
            "microsoft/TRELLIS.2-4B"
        )
        # H100 has 80GB; keep all models resident on GPU instead of
        # shuttling them CPU<->GPU on every call (default low_vram=True).
        self.pipeline.low_vram = False
        self.pipeline.cuda()
        print(
            f"Loaded TRELLIS.2 pipeline in {time.monotonic() - start:.1f}s", flush=True
        )

    @modal.method()
    def generate(
        self,
        image_bytes: bytes,
        image_name: str,
        pipeline_type: str = "1024_cascade",
        seed: int = 42,
        steps: int = 12,
        max_num_tokens: int = 49152,
    ) -> dict[str, str]:
        import torch  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        input_image = Image.open(io.BytesIO(image_bytes))

        start = time.monotonic()
        print(
            f"Running TRELLIS.2 generation with pipeline_type={pipeline_type}, "
            f"steps={steps}, max_num_tokens={max_num_tokens}...",
            flush=True,
        )
        mesh = self.pipeline.run(
            input_image,
            seed=seed,
            pipeline_type=pipeline_type,
            shape_slat_sampler_params={"steps": steps},
            tex_slat_sampler_params={"steps": steps},
            max_num_tokens=max_num_tokens,
        )[0]
        print(f"Generated mesh in {time.monotonic() - start:.1f}s", flush=True)

        start = time.monotonic()
        mesh.simplify(4_194_304)
        print(f"Simplified mesh in {time.monotonic() - start:.1f}s", flush=True)

        voxel_size = mesh.voxel_size
        if hasattr(voxel_size, "tolist"):
            voxel_size = voxel_size.tolist()

        stem = Path(image_name).stem or "image"
        intermediate_path = f"{INTERMEDIATE_DIR}/{stem}-{uuid.uuid4().hex[:8]}.pt"
        output_path = Path(OUTPUT_DIR) / intermediate_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        torch.save(
            {
                "vertices": mesh.vertices.detach().cpu(),
                "faces": mesh.faces.detach().cpu(),
                "attrs": mesh.attrs.detach().cpu().half(),
                "coords": mesh.coords.detach().cpu(),
                # Tuples instead of slices so torch.load(weights_only=True) works.
                "layout": {
                    name: (sli.start, sli.stop) for name, sli in mesh.layout.items()
                },
                "voxel_size": voxel_size,
            },
            output_path,
        )
        output_volume.commit()
        print(
            f"Saved intermediate mesh ({output_path.stat().st_size} bytes) to "
            f"{intermediate_path} in {time.monotonic() - start:.1f}s",
            flush=True,
        )

        return {"intermediate_path": intermediate_path}


@app.cls(
    image=image,
    gpu=POSTPROCESS_GPU,
    cpu=POSTPROCESS_CPU,
    timeout=60 * 60,
    scaledown_window=300,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
    volumes={OUTPUT_DIR: output_volume},
)
class Trellis2Postprocess:
    """Stage 2: mesh + attribute volume -> textured GLB (CPU-bound xatlas)."""

    @modal.enter(snap=True)
    def warmup(self) -> None:
        """Run a tiny mesh through to_glb so its kernels compile into the snapshot."""
        import o_voxel  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
        import trimesh  # type: ignore[import-not-found]

        start = time.monotonic()
        print("Warming up GLB postprocessing kernels...", flush=True)
        try:
            sphere = trimesh.creation.icosphere(subdivisions=4, radius=0.35)
            vertices = torch.tensor(sphere.vertices, dtype=torch.float32, device="cuda")
            faces = torch.tensor(sphere.faces, dtype=torch.int32, device="cuda")
            grid_size = 64
            coords = (
                ((vertices + 0.5) * grid_size)
                .long()
                .clamp(0, grid_size - 1)
                .unique(dim=0)
                .int()
            )
            attrs = torch.full((coords.shape[0], 6), 0.5, device="cuda")
            attrs[:, 5] = 1.0  # opaque alpha
            o_voxel.postprocess.to_glb(
                vertices=vertices,
                faces=faces,
                attr_volume=attrs,
                coords=coords,
                attr_layout=_to_attr_layout(PBR_ATTR_LAYOUT),
                voxel_size=1.0 / grid_size,
                aabb=AABB,
                decimation_target=20_000,
                texture_size=512,
                remesh=True,
                remesh_band=1,
                remesh_project=0,
                verbose=False,
            )
            print(f"Warmup done in {time.monotonic() - start:.1f}s", flush=True)
        except Exception as exc:
            print(f"Warmup failed (continuing): {exc}", flush=True)

    @modal.method()
    def convert_to_glb(
        self,
        intermediate_path: str,
        texture_size: int = 2048,
        decimation_target: int = 1_000_000,
    ) -> dict[str, int | str]:
        import o_voxel  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]

        # See the intermediate committed by the generate stage.
        output_volume.reload()
        source_path = Path(OUTPUT_DIR) / intermediate_path
        payload = torch.load(source_path, map_location="cuda")

        start = time.monotonic()
        print(
            f"Converting mesh to GLB with decimation_target={decimation_target}, "
            f"texture_size={texture_size}...",
            flush=True,
        )
        glb = o_voxel.postprocess.to_glb(
            vertices=payload["vertices"],
            faces=payload["faces"],
            attr_volume=payload["attrs"].float(),
            coords=payload["coords"],
            attr_layout=_to_attr_layout(payload["layout"]),
            voxel_size=payload["voxel_size"],
            aabb=AABB,
            decimation_target=decimation_target,
            texture_size=texture_size,
            remesh=True,
            remesh_band=1,
            remesh_project=0,
            verbose=True,
        )
        print(f"Converted mesh to GLB in {time.monotonic() - start:.1f}s", flush=True)

        volume_path = f"{Path(intermediate_path).stem}.glb"
        output_path = Path(OUTPUT_DIR) / volume_path
        glb.export(output_path, extension_webp=False)
        source_path.unlink(missing_ok=True)
        output_volume.commit()
        print(f"Saved GLB to Modal volume: {volume_path}", flush=True)

        return {
            "volume_path": volume_path,
            "size_bytes": output_path.stat().st_size,
        }


def _to_attr_layout(layout: dict[str, tuple[int, int]]) -> dict[str, slice]:
    """Rebuild the slice-based attribute layout expected by o_voxel.

    Args:
        layout: Mapping of attribute name to (start, stop) channel indices.

    Returns:
        Mapping of attribute name to channel slice.
    """

    return {name: slice(start, stop) for name, (start, stop) in layout.items()}
