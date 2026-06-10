import io
import time
import uuid
from pathlib import Path

import modal
from build_image import app, image

GPU = "H100"
OUTPUT_DIR = "/outputs"

output_volume = modal.Volume.from_name("trellis2-outputs", create_if_missing=True)


@app.cls(
    image=image,
    gpu=GPU,
    timeout=60 * 60,
    scaledown_window=10,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
    volumes={OUTPUT_DIR: output_volume},
)
class Trellis2:
    @modal.enter(snap=True)
    def load(self) -> None:
        from PIL import Image, ImageDraw  # type: ignore[import-not-found]
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

        start = time.monotonic()
        print("Running warmup generation...", flush=True)
        try:
            warmup_image = Image.new("RGB", (512, 512), (255, 255, 255))
            ImageDraw.Draw(warmup_image).ellipse(
                (128, 128, 384, 384), fill=(120, 120, 120)
            )
            self.pipeline.run(warmup_image, seed=0, pipeline_type="1024_cascade")
            print(f"Warmup done in {time.monotonic() - start:.1f}s", flush=True)
        except Exception as exc:
            print(f"Warmup failed (continuing): {exc}", flush=True)

    @modal.method()
    def generate_glb(
        self,
        image_bytes: bytes,
        image_name: str,
        pipeline_type: str = "1024_cascade",
        seed: int = 42,
        texture_size: int = 2048,
        decimation_target: int = 1_000_000,
        steps: int = 12,
        max_num_tokens: int = 49152,
    ) -> dict[str, int | str]:
        import o_voxel  # type: ignore[import-not-found]
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

        start = time.monotonic()
        print("Converting mesh to GLB...", flush=True)
        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices,
            faces=mesh.faces,
            attr_volume=mesh.attrs,
            coords=mesh.coords,
            attr_layout=mesh.layout,
            voxel_size=mesh.voxel_size,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=decimation_target,
            texture_size=texture_size,
            remesh=True,
            remesh_band=1,
            remesh_project=0,
            verbose=True,
        )
        print(f"Converted mesh to GLB in {time.monotonic() - start:.1f}s", flush=True)

        stem = Path(image_name).stem or "image"
        volume_path = f"{stem}-{uuid.uuid4().hex[:8]}.glb"
        output_path = Path(OUTPUT_DIR) / volume_path
        glb.export(output_path, extension_webp=False)
        output_volume.commit()
        print(f"Saved GLB to Modal volume: {volume_path}", flush=True)

        return {
            "volume_path": volume_path,
            "size_bytes": output_path.stat().st_size,
        }
