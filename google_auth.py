import json
import os

from google.oauth2.service_account import Credentials as ServiceAccountCredentials


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
