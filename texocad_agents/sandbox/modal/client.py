from dataclasses import dataclass

import modal

MODAL_APP_NAME = "image-to-3d-trellis2"
GENERATE_CLS_NAME = "Trellis2Generate"
POSTPROCESS_CLS_NAME = "Trellis2Postprocess"
OUTPUT_VOLUME_NAME = "trellis2-outputs"

DEFAULT_QUALITY = "draft"
DEFAULT_SEED = 42
DEFAULT_MAX_NUM_TOKENS = 49152


@dataclass(frozen=True)
class QualityPreset:
    """Bundle of generation settings for one quality level."""

    pipeline_type: str
    steps: int
    decimation_target: int
    texture_size: int


# Presets trade mesh/texture density for turnaround time; CPU-side UV
# unwrapping dominates wall time and scales with decimation_target.
QUALITY_PRESETS: dict[str, QualityPreset] = {
    "draft": QualityPreset(
        pipeline_type="512",
        steps=12,
        decimation_target=400_000,
        texture_size=2048,
    ),
    "high": QualityPreset(
        pipeline_type="1024_cascade",
        steps=12,
        decimation_target=1_000_000,
        texture_size=2048,
    ),
}


def generate_glb(
    image_bytes: bytes,
    image_name: str,
    *,
    quality: str = DEFAULT_QUALITY,
    seed: int = DEFAULT_SEED,
    max_num_tokens: int = DEFAULT_MAX_NUM_TOKENS,
    pipeline_type: str | None = None,
    steps: int | None = None,
    decimation_target: int | None = None,
    texture_size: int | None = None,
) -> bytes:
    """Generate a GLB mesh from an image via the deployed Modal TRELLIS.2 app.

    Runs two stages: mesh generation on a large GPU, then GLB postprocessing
    (remeshing, UV unwrapping, texture baking) on a cheaper GPU.

    Args:
        image_bytes: Source image bytes (PNG, JPEG, or WEBP).
        image_name: Original filename, used to name the GLB on the Modal volume.
        quality: Preset key from QUALITY_PRESETS ("draft" or "high").
        seed: Random seed for generation.
        max_num_tokens: Maximum number of tokens for sparse structure sampling.
        pipeline_type: Override for the preset's TRELLIS.2 pipeline variant.
        steps: Override for the preset's sampler steps.
        decimation_target: Override for the preset's target mesh face count.
        texture_size: Override for the preset's output texture resolution.

    Returns:
        GLB file contents downloaded from the Modal output volume.

    Raises:
        ValueError: If quality is not a known preset.
        RuntimeError: If the deployed Modal app cannot be found.
    """

    preset = QUALITY_PRESETS.get(quality)
    if preset is None:
        raise ValueError(
            f"Unknown quality {quality!r}; valid options: {sorted(QUALITY_PRESETS)}"
        )

    generator = modal.Cls.from_name(MODAL_APP_NAME, GENERATE_CLS_NAME)()
    postprocessor = modal.Cls.from_name(MODAL_APP_NAME, POSTPROCESS_CLS_NAME)()
    try:
        generation = generator.generate.remote(
            image_bytes,
            image_name,
            pipeline_type=pipeline_type or preset.pipeline_type,
            seed=seed,
            steps=steps or preset.steps,
            max_num_tokens=max_num_tokens,
        )
        conversion = postprocessor.convert_to_glb.remote(
            str(generation["intermediate_path"]),
            texture_size=texture_size or preset.texture_size,
            decimation_target=decimation_target or preset.decimation_target,
        )
    except modal.exception.NotFoundError as exc:
        raise RuntimeError(
            f"Modal app {MODAL_APP_NAME!r} is not deployed. Deploy it with: "
            "modal deploy texocad_agents/sandbox/modal/trellis.py"
        ) from exc

    output_volume = modal.Volume.from_name(OUTPUT_VOLUME_NAME)
    volume_path = str(conversion["volume_path"])
    return b"".join(output_volume.read_file(volume_path))
