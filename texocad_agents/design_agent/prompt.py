from pathlib import Path

PROMPT_DIR = Path(__file__).with_name("prompt")
PROMPT_FILES = (
    "workflow_prompt.md",
    "quality_check.md",
    "behaviours_prompt.md",
    "build123d_prompt.md",
)


def load_system_prompt() -> str:
    return "\n\n".join(
        (PROMPT_DIR / prompt_file).read_text(encoding="utf-8").strip()
        for prompt_file in PROMPT_FILES
    )


SYSTEM_PROMPT = load_system_prompt()
