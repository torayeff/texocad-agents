import os

import modal

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = modal.App("image-to-3d-trellis2")
gpu = "H100"

hf_secret = modal.Secret.from_dict(
    {"HF_TOKEN": os.environ["HF_TOKEN"]} if os.environ.get("HF_TOKEN") else {}
)

image = (
    modal.Image
    .from_registry(
        "nvidia/cuda:12.4.0-devel-ubuntu22.04",
        add_python="3.10",
    )
    .entrypoint([])
    .apt_install(
        "build-essential",
        "clang",
        "cmake",
        "git",
        "libgl1",
        "libglib2.0-0",
        "libjpeg-dev",
    )
    .env({
        "ATTN_BACKEND": "flash_attn",
        "CUDA_HOME": "/usr/local/cuda",
        "HF_HOME": "/cache/huggingface",
        "HF_HUB_CACHE": "/cache/huggingface",
        "HF_XET_HIGH_PERFORMANCE": "1",
        "OPENCV_IO_ENABLE_OPENEXR": "1",
        "PYTHONPATH": "/opt/TRELLIS.2",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "SPARSE_CONV_BACKEND": "flex_gemm",
    })
    .pip_install(
        "torch==2.6.0",
        "torchvision==0.21.0",
        index_url="https://download.pytorch.org/whl/cu124",
    )
    .pip_install(
        "accelerate",
        "easydict",
        "hf_xet",
        "huggingface_hub",
        "imageio",
        "imageio-ffmpeg",
        "kornia",
        "lpips",
        "ninja",
        "opencv-python-headless",
        "pandas",
        "plyfile",
        "safetensors",
        "timm",
        "tqdm",
        "transformers==4.56.0",
        "trimesh",
        "wheel",
        "zstandard",
    )
    .pip_install(
        "git+https://github.com/EasternJournalist/utils3d.git@9a4eb15e4021b67b12c460c7057d642626897ec8"
    )
    .pip_install("flash-attn==2.7.3", extra_options="--no-build-isolation", gpu=gpu)
    .run_commands(
        "git clone --recursive https://github.com/microsoft/TRELLIS.2.git /opt/TRELLIS.2"
    )
    .run_commands(
        "mkdir -p /tmp/extensions && "
        "git clone -b v0.4.0 https://github.com/NVlabs/nvdiffrast.git /tmp/extensions/nvdiffrast && "
        "pip install /tmp/extensions/nvdiffrast --no-build-isolation",
        gpu=gpu,
    )
    .run_commands(
        "mkdir -p /tmp/extensions && "
        "git clone https://github.com/JeffreyXiang/CuMesh.git /tmp/extensions/CuMesh --recursive && "
        "pip install /tmp/extensions/CuMesh --no-build-isolation",
        gpu=gpu,
    )
    .run_commands(
        "mkdir -p /tmp/extensions && "
        "git clone https://github.com/JeffreyXiang/FlexGEMM.git /tmp/extensions/FlexGEMM --recursive && "
        "pip install /tmp/extensions/FlexGEMM --no-build-isolation",
        gpu=gpu,
    )
    .run_commands(
        "pip install /opt/TRELLIS.2/o-voxel --no-build-isolation --no-deps",
        gpu=gpu,
    )
    .run_commands(
        "python - <<'PY'\n"
        "from huggingface_hub import snapshot_download\n"
        "snapshot_download(repo_id='microsoft/TRELLIS.2-4B')\n"
        "snapshot_download(repo_id='microsoft/TRELLIS-image-large')\n"
        "snapshot_download(repo_id='facebook/dinov3-vitl16-pretrain-lvd1689m')\n"
        "snapshot_download(repo_id='briaai/RMBG-2.0')\n"
        "PY",
        secrets=[hf_secret],
    )
    .env({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    })
    .add_local_python_source("build_image")
)


if __name__ == "__main__":
    with modal.enable_output(), app.run():
        image.build(app)
