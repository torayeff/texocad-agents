from pathlib import Path

PROMPT_DIR = Path(__file__).with_name("prompt")
SYSTEM_PROMPT_FILES = (
    "workflow_prompt.md",
    "image_prompt.md",
)


SYSTEM_PROMPT = "\n\n".join(
    (PROMPT_DIR / prompt_file).read_text(encoding="utf-8").strip()
    for prompt_file in SYSTEM_PROMPT_FILES
)

IMAGE_PROMPT_TEMPLATE = (
    (PROMPT_DIR / "image_prompt.md").read_text(encoding="utf-8").strip()
)
