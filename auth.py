import asyncio
import os
from typing import Any, NoReturn

import jwt
from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from design_agent.conf import DEFAULT_MODEL, MODELS

auth = Auth()

JWT_CLAIM_AUTHORIZED_PARTY = "azp"
JWT_CLAIM_SESSION_ID = "sid"
JWT_CLAIM_SESSION_STATUS = "sts"
JWT_CLAIM_SUBJECT = "sub"
JWT_CLAIM_TOKEN_VERSION = "v"
TOKEN_VERSION = 2
SESSION_STATUS_PENDING = "pending"
REQUIRED_CLAIMS: tuple[str, ...] = (
    "exp",
    "iat",
    "iss",
    "nbf",
    JWT_CLAIM_SESSION_ID,
    JWT_CLAIM_SUBJECT,
    JWT_CLAIM_TOKEN_VERSION,
)
JWT_LEEWAY_SECONDS = 5

ACCESS_CLAIM_FREE = "freeAccess"
ACCESS_CLAIM_PLANS = "pla"
ACCESS_CLAIM_VIP = "vipAccess"
ACCESS_BYPASS_CLAIMS = (ACCESS_CLAIM_FREE, ACCESS_CLAIM_VIP)
EARLY_BIRD_PLAN = "early_bird"
ALLOWED_PLAN_SCOPES: frozenset[str] = frozenset(
    (
        f"u:{EARLY_BIRD_PLAN}",
        f"o:{EARLY_BIRD_PLAN}",
    )
)

METADATA_KEY = "metadata"
METADATA_ALLOWED_MODELS = "allowed_models"
METADATA_MODEL = "model"
METADATA_OWNER = "owner"


def _unauthorized(detail: str) -> NoReturn:
    raise Auth.exceptions.HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> NoReturn:
    raise Auth.exceptions.HTTPException(status_code=403, detail=detail)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _csv_parts(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_audience(raw: str) -> str | list[str] | None:
    parts = _csv_parts(raw)
    if len(parts) == 1:
        return parts[0]
    if parts:
        return parts
    return None


_jwks = jwt.PyJWKClient(_required_env("CLERK_JWKS_URL"), cache_keys=True)
_issuer = _required_env("CLERK_ISSUER")
_allowed_azp = set(_csv_parts(_required_env("ALLOWED_TOKEN_AZP")))
if not _allowed_azp:
    raise RuntimeError("ALLOWED_TOKEN_AZP must contain at least one origin")
_audience = _parse_audience(os.getenv("CLERK_AUDIENCE", ""))


def verify_token(token: str) -> tuple[str, dict[str, object]]:
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

    azp = payload.get(JWT_CLAIM_AUTHORIZED_PARTY)
    if not isinstance(azp, str) or azp not in _allowed_azp:
        _unauthorized("Invalid authorized party")

    for claim in (JWT_CLAIM_SESSION_ID, JWT_CLAIM_SUBJECT):
        if not isinstance(payload.get(claim), str) or not payload[claim]:
            _unauthorized(f"Token missing {claim} claim")

    if payload.get(JWT_CLAIM_TOKEN_VERSION) != TOKEN_VERSION:
        _unauthorized("Unsupported token version")

    if payload.get(JWT_CLAIM_SESSION_STATUS) == SESSION_STATUS_PENDING:
        _unauthorized("Pending sessions are not allowed")

    check_subscription(payload)
    return payload[JWT_CLAIM_SUBJECT], payload


def check_subscription(payload: dict[str, object]) -> None:
    if any(payload.get(claim) is True for claim in ACCESS_BYPASS_CLAIMS):
        return

    pla = payload.get(ACCESS_CLAIM_PLANS)
    if not isinstance(pla, str):
        _forbidden("Active subscription required")
    plans = set(_csv_parts(pla))
    if plans.isdisjoint(ALLOWED_PLAN_SCOPES):
        _forbidden("Active subscription required")


def allowed_models(payload: dict[str, object]) -> set[str]:
    if payload.get(ACCESS_CLAIM_VIP) is True:
        return set(MODELS.keys())
    return {DEFAULT_MODEL}


def _owner_filter(ctx: Auth.types.AuthContext) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    return {METADATA_OWNER: ctx.user.identity}


def _stamp_owner(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    owner_filter: Auth.types.FilterType = {METADATA_OWNER: ctx.user.identity}
    metadata = value.setdefault(METADATA_KEY, {})
    metadata.update(owner_filter)
    return owner_filter


@auth.authenticate
async def authenticate(
    authorization: str | None = None,
) -> Auth.types.MinimalUserDict:
    if not authorization:
        _unauthorized("Missing or invalid Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        _unauthorized("Missing or invalid Authorization header")

    try:
        user_id, payload = await asyncio.to_thread(verify_token, token)
    except jwt.ExpiredSignatureError:
        _unauthorized("Token expired")
    except jwt.PyJWTError as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return {
        "identity": user_id,
        METADATA_KEY: {METADATA_ALLOWED_MODELS: sorted(allowed_models(payload))},
    }


@auth.on
async def reject_unhandled_requests(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    _forbidden("Unsupported resource or action")


@auth.on.threads.create
async def authorize_thread_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create.value,
) -> Auth.types.FilterType:
    return _stamp_owner(ctx, value)


@auth.on.threads.read
async def authorize_thread_read(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.read.value,
) -> Auth.types.FilterType:
    return _owner_filter(ctx)


@auth.on.threads.search
async def authorize_thread_search(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.search.value,
) -> Auth.types.FilterType:
    return _owner_filter(ctx)


@auth.on.threads.delete
async def authorize_thread_delete(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.delete.value,
) -> Auth.types.FilterType:
    return _owner_filter(ctx)


@auth.on.threads.create_run
async def authorize_thread_run_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create_run.value,
) -> Auth.types.FilterType:
    if is_studio_user(ctx.user):
        return {}
    allowed = set(ctx.user.get(METADATA_KEY, {}).get(METADATA_ALLOWED_MODELS, []))
    metadata = value.setdefault(METADATA_KEY, {})
    requested = metadata.get(METADATA_MODEL, DEFAULT_MODEL)
    if requested not in allowed:
        _forbidden("Requested model is not available on your plan")
    metadata[METADATA_MODEL] = requested
    metadata[METADATA_OWNER] = ctx.user.identity
    return {METADATA_OWNER: ctx.user.identity}
