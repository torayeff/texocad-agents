"""Identity verification for Clerk LangGraph callers."""

import asyncio
import os
from typing import NoReturn

import jwt
from langgraph_sdk import Auth

from texocad_agents.auth.policies import FREE_REASONING_EFFORTS
from texocad_agents.models import ALLOWED_REASONING_EFFORTS, DEFAULT_MODEL, MODELS

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
METADATA_ALLOWED_REASONING_EFFORTS = "allowed_reasoning_efforts"
METADATA_HAS_ACCESS_BYPASS = "has_access_bypass"
METADATA_HAS_ALLOWED_PLAN = "has_allowed_plan"
METADATA_IS_VIP = "is_vip"
METADATA_MODEL = "model"
METADATA_OWNER = "owner"
METADATA_REASONING_EFFORT = "reasoning_effort"

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


CLERK_JWKS_URL = _require_env("CLERK_JWKS_URL")
CLERK_ISSUER = _require_env("CLERK_ISSUER")
ALLOWED_TOKEN_AZP = _require_env("ALLOWED_TOKEN_AZP")

_jwks = jwt.PyJWKClient(CLERK_JWKS_URL, cache_keys=True)
_allowed_azp = {part.strip() for part in ALLOWED_TOKEN_AZP.split(",") if part.strip()}
if not _allowed_azp:
    raise RuntimeError("ALLOWED_TOKEN_AZP must contain at least one origin")


async def authenticate_identity(
    authorization: str | None,
) -> Auth.types.MinimalUserDict:
    """Validate a Clerk bearer token and return LangGraph user metadata."""

    token = _bearer_token(authorization)
    if not token:
        _unauthorized("Missing Authorization header")
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
        METADATA_KEY: _clerk_metadata(payload),
    }


def verify_token(token: str) -> tuple[str, dict[str, object]]:
    """Verify a Clerk JWT and return the authenticated subject."""

    signing_key = _jwks.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=CLERK_ISSUER,
        leeway=JWT_LEEWAY_SECONDS,
        options={
            "require": list(REQUIRED_CLAIMS),
            "verify_aud": False,
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

    return payload[JWT_CLAIM_SUBJECT], payload


def allowed_models(payload: dict[str, object]) -> set[str]:
    """Return model keys available to a validated token payload."""

    if payload.get(ACCESS_CLAIM_VIP) is True:
        return set(MODELS.keys())
    return {DEFAULT_MODEL}


def allowed_reasoning_efforts(payload: dict[str, object]) -> set[str]:
    """Return reasoning efforts available to a validated token payload."""

    if payload.get(ACCESS_CLAIM_VIP) is True or has_allowed_plan(payload):
        return set(ALLOWED_REASONING_EFFORTS)
    return set(FREE_REASONING_EFFORTS)


def has_allowed_plan(payload: dict[str, object]) -> bool:
    """Return whether the token payload includes an allowed subscription plan."""

    pla = payload.get(ACCESS_CLAIM_PLANS)
    if not isinstance(pla, str):
        return False
    plans = {part.strip() for part in pla.split(",") if part.strip()}
    return not plans.isdisjoint(ALLOWED_PLAN_SCOPES)


def _clerk_metadata(payload: dict[str, object]) -> dict[str, object]:
    has_plan = has_allowed_plan(payload)
    is_vip = payload.get(ACCESS_CLAIM_VIP) is True
    has_access_bypass = any(
        payload.get(claim) is True for claim in ACCESS_BYPASS_CLAIMS
    )
    return {
        METADATA_ALLOWED_MODELS: sorted(allowed_models(payload)),
        METADATA_ALLOWED_REASONING_EFFORTS: sorted(allowed_reasoning_efforts(payload)),
        METADATA_HAS_ACCESS_BYPASS: has_access_bypass,
        METADATA_HAS_ALLOWED_PLAN: has_plan,
        METADATA_IS_VIP: is_vip,
    }


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer":
        if not token:
            _unauthorized("Missing or invalid Authorization header")
        return token
    return None


def _unauthorized(detail: str) -> NoReturn:
    raise Auth.exceptions.HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
