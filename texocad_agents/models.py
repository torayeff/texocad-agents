"""Configure and construct chat models for TexoCAD agents."""

from dataclasses import dataclass

import httpx
from langchain.chat_models import init_chat_model

MODEL_TIMEOUT = httpx.Timeout(connect=30.0, read=1800.0, write=30.0, pool=30.0)
MAX_RETRIES = 3
DEFAULT_MODEL = "gpt-5.5"


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for a supported chat model.

    Attributes:
        provider: LangChain model provider identifier.
        name: Provider-specific model name.
        kwargs: Additional provider options passed to `init_chat_model`.
    """

    provider: str
    name: str
    kwargs: dict


MODELS: dict[str, ModelConfig] = {
    "gpt-5.5": ModelConfig(
        provider="openai",
        name="gpt-5.5",
        kwargs={
            "reasoning": {"effort": "xhigh", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "store": False,
        },
    ),
}


def create_model(model_key: str | None = None):
    """Create a configured chat model by registry key.

    Args:
        model_key: Optional model key from `MODELS`. Defaults to
            `DEFAULT_MODEL` when omitted.

    Returns:
        Initialized LangChain chat model.

    Raises:
        ValueError: If `model_key` is not registered.
    """

    model_key = model_key or DEFAULT_MODEL

    if model_key not in MODELS:
        raise ValueError(f"Unknown model key: {model_key}")

    model_config = MODELS[model_key]

    return init_chat_model(
        model_config.name,
        model_provider=model_config.provider,
        max_retries=MAX_RETRIES,
        timeout=MODEL_TIMEOUT,
        **model_config.kwargs,
    )
