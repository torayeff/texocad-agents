import asyncio
import os
from typing import Any, NoReturn

import jwt
from langgraph_sdk import Auth

from texocad_agents.models import DEFAULT_MODEL, MODELS

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
ALLOWED_PLAN_SCOPES: frozenset[str] = frozenset((
    f"u:{EARLY_BIRD_PLAN}",
    f"o:{EARLY_BIRD_PLAN}",
))

METADATA_KEY = "metadata"
METADATA_ALLOWED_MODELS = "allowed_models"
METADATA_MODEL = "model"
METADATA_OWNER = "owner"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


CLERK_JWKS_URL = _require_env("CLERK_JWKS_URL")
CLERK_ISSUER = _require_env("CLERK_ISSUER")
ALLOWED_TOKEN_AZP = _require_env("ALLOWED_TOKEN_AZP")
CLERK_AUDIENCE = _require_env("CLERK_AUDIENCE")

_jwks = jwt.PyJWKClient(CLERK_JWKS_URL, cache_keys=True)
_allowed_azp = {part.strip() for part in ALLOWED_TOKEN_AZP.split(",") if part.strip()}
if not _allowed_azp:
    raise RuntimeError("ALLOWED_TOKEN_AZP must contain at least one origin")
_audience_parts = [part.strip() for part in CLERK_AUDIENCE.split(",") if part.strip()]
if not _audience_parts:
    raise RuntimeError("CLERK_AUDIENCE must contain at least one audience")
_audience: str | list[str] = (
    _audience_parts[0] if len(_audience_parts) == 1 else _audience_parts
)

auth = Auth()


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
    allowed = set(ctx.user.get(METADATA_KEY, {}).get(METADATA_ALLOWED_MODELS, []))
    metadata = value.setdefault(METADATA_KEY, {})
    requested = metadata.get(METADATA_MODEL, DEFAULT_MODEL)
    if requested not in allowed:
        _forbidden("Requested model is not available on your plan")
    metadata[METADATA_MODEL] = requested
    metadata[METADATA_OWNER] = ctx.user.identity
    return {METADATA_OWNER: ctx.user.identity}


def verify_token(token: str) -> tuple[str, dict[str, object]]:
    signing_key = _jwks.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=_audience,
        issuer=CLERK_ISSUER,
        leeway=JWT_LEEWAY_SECONDS,
        options={
            "require": list(REQUIRED_CLAIMS),
            "verify_aud": True,
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
    plans = {part.strip() for part in pla.split(",") if part.strip()}
    if plans.isdisjoint(ALLOWED_PLAN_SCOPES):
        _forbidden("Active subscription required")


def allowed_models(payload: dict[str, object]) -> set[str]:
    if payload.get(ACCESS_CLAIM_VIP) is True:
        return set(MODELS.keys())
    return {DEFAULT_MODEL}


def _owner_filter(ctx: Auth.types.AuthContext) -> Auth.types.FilterType:
    return {METADATA_OWNER: ctx.user.identity}


def _stamp_owner(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> Auth.types.FilterType:
    owner_filter: Auth.types.FilterType = {METADATA_OWNER: ctx.user.identity}
    metadata = value.setdefault(METADATA_KEY, {})
    metadata.update(owner_filter)
    return owner_filter


def _unauthorized(detail: str) -> NoReturn:
    raise Auth.exceptions.HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> NoReturn:
    raise Auth.exceptions.HTTPException(status_code=403, detail=detail)
