import asyncio
import os
from typing import Any

import jwt
from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from design_agent.conf import DEFAULT_MODEL, MODELS

auth = Auth()

EARLY_BIRD_PLAN = "early_bird"
EARLY_BIRD_PLAN_SCOPED: frozenset[str] = frozenset((
    f"u:{EARLY_BIRD_PLAN}",
    f"o:{EARLY_BIRD_PLAN}",
))
TOKEN_VERSION = 2
REQUIRED_CLAIMS: tuple[str, ...] = (
    "exp",
    "iat",
    "iss",
    "nbf",
    "sid",
    "sub",
    "v",
)
JWT_LEEWAY_SECONDS = 5

_jwks_url = os.getenv("CLERK_JWKS_URL")
if not _jwks_url:
    raise RuntimeError("CLERK_JWKS_URL is not set")
_jwks = jwt.PyJWKClient(_jwks_url, cache_keys=True)

_issuer = os.getenv("CLERK_ISSUER")
if not _issuer:
    raise RuntimeError("CLERK_ISSUER is not set")

_azp_raw = os.getenv("ALLOWED_TOKEN_AZP")
if not _azp_raw:
    raise RuntimeError("ALLOWED_TOKEN_AZP is not set")
_allowed_azp = {s.strip() for s in _azp_raw.split(",") if s.strip()}
if not _allowed_azp:
    raise RuntimeError("ALLOWED_TOKEN_AZP must contain at least one origin")

_aud_raw = os.getenv("CLERK_AUDIENCE", "").strip()
_audience: str | list[str] | None = None
if _aud_raw:
    _aud_parts = [s.strip() for s in _aud_raw.split(",") if s.strip()]
    if len(_aud_parts) == 1:
        _audience = _aud_parts[0]
    elif _aud_parts:
        _audience = _aud_parts


def verify_token(token: str) -> tuple[str, dict]:
    signing_key = _jwks.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=_audience,
        issuer=_issuer,
        leeway=JWT_LEEWAY_SECONDS,
        options={
            "require": list(REQUIRED_CLAIMS),
            "verify_aud": _audience is not None,
        },
    )

    azp = payload.get("azp")
    if not isinstance(azp, str) or azp not in _allowed_azp:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Invalid authorized party",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for claim in ("sid", "sub"):
        if not isinstance(payload.get(claim), str) or not payload[claim]:
            raise Auth.exceptions.HTTPException(
                status_code=401,
                detail=f"Token missing {claim} claim",
                headers={"WWW-Authenticate": "Bearer"},
            )

    if payload.get("v") != TOKEN_VERSION:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Unsupported token version",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("sts") == "pending":
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Pending sessions are not allowed",
            headers={"WWW-Authenticate": "Bearer"},
        )

    check_subscription(payload)
    return payload["sub"], payload


def check_subscription(payload: dict[str, object]) -> None:
    if payload.get("vipAccess") is True:
        return
    if payload.get("freeAccess") is True:
        return

    pla = payload.get("pla")
    if not isinstance(pla, str):
        raise Auth.exceptions.HTTPException(
            status_code=403, detail="Active subscription required"
        )
    plans = {p.strip() for p in pla.split(",") if p.strip()}
    if plans.isdisjoint(EARLY_BIRD_PLAN_SCOPED):
        raise Auth.exceptions.HTTPException(
            status_code=403, detail="Active subscription required"
        )


def allowed_models(payload: dict[str, object]) -> set[str]:
    if payload.get("vipAccess") is True:
        return set(MODELS.keys())
    return {DEFAULT_MODEL}


def _owner_scoped(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
    *,
    stamp_owner: bool,
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    owner_filter: Auth.types.FilterType = {"owner": ctx.user.identity}
    if stamp_owner:
        metadata = value.setdefault("metadata", {})
        metadata.update(owner_filter)
    return owner_filter


@auth.authenticate
async def authenticate(
    authorization: str | None = None,
) -> Auth.types.MinimalUserDict:
    if not authorization:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id, payload = await asyncio.to_thread(verify_token, token)
    except jwt.ExpiredSignatureError:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.PyJWTError as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return {
        "identity": user_id,
        "metadata": {"allowed_models": list(allowed_models(payload))},
    }


@auth.on
async def reject_unhandled_requests(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    raise Auth.exceptions.HTTPException(
        status_code=403, detail="Unsupported resource or action"
    )


@auth.on.threads.create
async def authorize_thread_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create.value,
) -> Auth.types.FilterType:
    return _owner_scoped(ctx, value, stamp_owner=True)


@auth.on.threads.read
async def authorize_thread_read(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.read.value,
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    return {"owner": ctx.user.identity}


@auth.on.threads.search
async def authorize_thread_search(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.search.value,
) -> Auth.types.FilterType:
    return _owner_scoped(ctx, value, stamp_owner=False)


@auth.on.threads.delete
async def authorize_thread_delete(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.delete.value,
) -> Auth.types.FilterType:
    return _owner_scoped(ctx, value, stamp_owner=False)


@auth.on.threads.create_run
async def authorize_thread_run_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create_run.value,
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    allowed = set(ctx.user.get("metadata", {}).get("allowed_models", []))
    metadata = value.setdefault("metadata", {})
    requested = metadata.get("model", DEFAULT_MODEL)
    if requested not in allowed:
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail="Requested model is not available on your plan",
        )
    metadata["model"] = requested
    metadata["owner"] = ctx.user.identity
    return {"owner": ctx.user.identity}
