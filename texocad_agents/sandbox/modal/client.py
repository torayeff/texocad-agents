import modal

MODAL_APP_NAME = "image-to-3d-trellis2"
MODAL_CLS_NAME = "Trellis2"
OUTPUT_VOLUME_NAME = "trellis2-outputs"

DEFAULT_PIPELINE_TYPE = "1024_cascade"
DEFAULT_SEED = 42
DEFAULT_TEXTURE_SIZE = 2048
DEFAULT_DECIMATION_TARGET = 1_000_000
DEFAULT_STEPS = 12
DEFAULT_MAX_NUM_TOKENS = 49152


def generate_glb(
    image_bytes: bytes,
    image_name: str,
    *,
    pipeline_type: str = DEFAULT_PIPELINE_TYPE,
    seed: int = DEFAULT_SEED,
    texture_size: int = DEFAULT_TEXTURE_SIZE,
    decimation_target: int = DEFAULT_DECIMATION_TARGET,
    steps: int = DEFAULT_STEPS,
    max_num_tokens: int = DEFAULT_MAX_NUM_TOKENS,
) -> bytes:
    """Generate a GLB mesh from an image via the deployed Modal TRELLIS.2 app.

    Args:
        image_bytes: Source image bytes (PNG, JPEG, or WEBP).
        image_name: Original filename, used to name the GLB on the Modal volume.
        pipeline_type: TRELLIS.2 pipeline variant to run.
        seed: Random seed for generation.
        texture_size: Output texture resolution.
        decimation_target: Target face count for mesh decimation.
        steps: Sampler steps for shape and texture latents.
        max_num_tokens: Maximum number of tokens for sparse structure sampling.

    Returns:
        GLB file contents downloaded from the Modal output volume.

    Raises:
        RuntimeError: If the deployed Modal app cannot be found.
    """

    trellis2 = modal.Cls.from_name(MODAL_APP_NAME, MODAL_CLS_NAME)()
    try:
        generation = trellis2.generate_glb.remote(
            image_bytes,
            image_name,
            pipeline_type=pipeline_type,
            seed=seed,
            texture_size=texture_size,
            decimation_target=decimation_target,
            steps=steps,
            max_num_tokens=max_num_tokens,
        )
    except modal.exception.NotFoundError as exc:
        raise RuntimeError(
            f"Modal app {MODAL_APP_NAME!r} is not deployed. Deploy it with: "
            "modal deploy texocad_agents/sandbox/modal/trellis.py"
        ) from exc

    output_volume = modal.Volume.from_name(OUTPUT_VOLUME_NAME)
    volume_path = str(generation["volume_path"])
    return b"".join(output_volume.read_file(volume_path))
