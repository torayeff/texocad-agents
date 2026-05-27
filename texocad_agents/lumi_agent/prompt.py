from pathlib import Path

PROMPT_DIR = Path(__file__).with_name("prompt")
RING_PROMPT_DIR = PROMPT_DIR / "ring"
PROMPT_FILES = (
    "persona.md",
    "workflow_prompt.md",
    "rings.md",
)
IMAGE_PROMPT_FILES = ("image_prompt.md",)


def _read_prompt_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = "\n\n".join(
    _read_prompt_file(prompt_path)
    for prompt_path in (
        *(PROMPT_DIR / prompt_file for prompt_file in PROMPT_FILES),
        *sorted(RING_PROMPT_DIR.glob("*.md")),
        *(PROMPT_DIR / prompt_file for prompt_file in IMAGE_PROMPT_FILES),
    )
)
