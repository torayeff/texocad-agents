from pathlib import Path

PROMPT_DIR = Path(__file__).with_name("prompt")
SYSTEM_PROMPT_FILES = (
    "persona.md",
    "workflow_prompt.md",
    "design_principles.md",
    "image_prompt.md",
    "quality_check.md",
    "tool_contract.md",
)


SYSTEM_PROMPT = "\n\n".join(
    (PROMPT_DIR / prompt_file).read_text(encoding="utf-8").strip()
    for prompt_file in SYSTEM_PROMPT_FILES
)
