from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    name: str
    kwargs: dict


MODELS: dict[str, ModelConfig] = {
    "gpt-5.5": ModelConfig(
        provider="openai",
        name="gpt-5.5",
        kwargs={"reasoning": {"effort": "xhigh", "summary": "auto"}},
    ),
    "claude-opus-4-7": ModelConfig(
        provider="azure_anthropic",
        name="claude-opus-4-7",
        kwargs={
            "thinking": {"type": "adaptive", "display": "summarized"},
            "effort": "max",
        },
    ),
    "gemini-3.1-pro-preview": ModelConfig(
        provider="google_genai",
        name="gemini-3.1-pro-preview",
        kwargs={"include_thoughts": True, "vertexai": True},
    ),
}

DEFAULT_MODEL = "gpt-5.5"
