import json
import os
from dataclasses import dataclass

from google.oauth2.service_account import Credentials as ServiceAccountCredentials


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
