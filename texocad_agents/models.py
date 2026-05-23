from dataclasses import dataclass

import httpx
from langchain.chat_models import init_chat_model

MODEL_TIMEOUT = httpx.Timeout(connect=30.0, read=1800.0, write=30.0, pool=30.0)
MAX_RETRIES = 3


@dataclass(frozen=True)
class ModelConfig:
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

DEFAULT_MODEL = "gpt-5.5"


def create_model(model_key: str):
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
