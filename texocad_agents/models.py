import json
import os
from dataclasses import dataclass

import httpx
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from langchain.chat_models import init_chat_model
from langchain_anthropic import ChatAnthropic

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


def create_model(model_key: str):
    if model_key not in MODELS:
        raise ValueError(f"Unknown model key: {model_key}")

    model_config = MODELS[model_key]
    if model_config.provider == "azure_anthropic":
        api_key = os.environ["ANTHROPIC_FOUNDRY_API_KEY"]
        return ChatAnthropic(
            model=model_config.name,
            api_key=api_key,
            base_url=os.environ["ANTHROPIC_FOUNDRY_BASE_URL"],
            default_headers={"api-key": api_key},
            max_retries=MAX_RETRIES,
            timeout=1800.0,
            **model_config.kwargs,
        )

    return init_chat_model(
        model_config.name,
        model_provider=model_config.provider,
        max_retries=MAX_RETRIES,
        timeout=MODEL_TIMEOUT,
        **with_google_vertex_credentials(model_config.kwargs),
    )


def with_google_vertex_credentials(
    model_kwargs: dict[str, object],
) -> dict[str, object]:
    resolved_kwargs = dict(model_kwargs)
    if not resolved_kwargs.get("vertexai"):
        return resolved_kwargs

    credentials_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if not credentials_json:
        return resolved_kwargs

    try:
        service_account_info = json.loads(credentials_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GCP_SERVICE_ACCOUNT_JSON must contain a valid service account JSON object."
        ) from exc

    resolved_kwargs["credentials"] = (
        ServiceAccountCredentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    )

    return resolved_kwargs
