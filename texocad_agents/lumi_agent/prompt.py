from pathlib import Path

PROMPT_DIR = Path(__file__).with_name("prompt")
PROMPT_FILES = (
    "persona.md",
    "workflow.md",
    "cheatsheet.md",
)


SYSTEM_PROMPT = "\n\n".join(
    (PROMPT_DIR / prompt_file).read_text(encoding="utf-8").strip()
    for prompt_file in PROMPT_FILES
)
