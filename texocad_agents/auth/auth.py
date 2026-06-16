"""Authenticate users and authorize LangGraph agent resources."""

from typing import Any, NoReturn

from langgraph_sdk import Auth

from texocad_agents.auth.identity import (
    METADATA_HAS_ACCESS_BYPASS,
    METADATA_HAS_ALLOWED_PLAN,
    METADATA_IS_VIP,
    METADATA_KEY,
    METADATA_MODEL,
    METADATA_OWNER,
    METADATA_REASONING_EFFORT,
    authenticate_identity,
)
from texocad_agents.auth.policies import (
    METADATA_GRAPH_ID,
    AgentPolicyError,
    authorize_agent_run,
    resolve_graph_id,
)
from texocad_agents.models import DEFAULT_MODEL

auth = Auth()


@auth.authenticate
async def authenticate(
    authorization: str | None = None,
) -> Auth.types.MinimalUserDict:
    """Validate a Clerk bearer token and return LangGraph user metadata.

    Args:
        authorization: Raw `Authorization` header value.

    Returns:
        Minimal LangGraph user dictionary containing the owner identity and
        policy metadata.

    Raises:
        Auth.exceptions.HTTPException: If the bearer token is missing or token
            validation fails.
    """

    return await authenticate_identity(authorization)


@auth.on
async def reject_unhandled_requests(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> Auth.types.FilterType:
    """Reject LangGraph auth actions without an explicit handler.

    Args:
        ctx: LangGraph authentication context.
        value: Resource payload for the unhandled action.

    Returns:
        This function never returns successfully.

    Raises:
        Auth.exceptions.HTTPException: Always raised with status 403.
    """

    _forbidden("Unsupported resource or action")


@auth.on.threads.create
async def authorize_thread_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create.value,
) -> Auth.types.FilterType:
    """Authorize thread creation and stamp owner metadata.

    Args:
        ctx: LangGraph authentication context.
        value: Thread creation payload to mutate with owner metadata.

    Returns:
        Metadata filter binding the new thread to the authenticated owner.
    """

    return _stamp_owner(ctx, value)


@auth.on.threads.read
async def authorize_thread_read(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.read.value,
) -> Auth.types.FilterType:
    """Authorize thread reads for the authenticated owner only.

    Args:
        ctx: LangGraph authentication context.
        value: Thread read payload.

    Returns:
        Metadata filter requiring owner equality.
    """

    return _owner_filter(ctx)


@auth.on.threads.search
async def authorize_thread_search(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.search.value,
) -> Auth.types.FilterType:
    """Authorize thread search results for the authenticated owner only.

    Args:
        ctx: LangGraph authentication context.
        value: Thread search payload.

    Returns:
        Metadata filter requiring owner equality.
    """

    return _owner_filter(ctx)


@auth.on.threads.delete
async def authorize_thread_delete(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.delete.value,
) -> Auth.types.FilterType:
    """Authorize thread deletion for the authenticated owner only.

    Args:
        ctx: LangGraph authentication context.
        value: Thread deletion payload.

    Returns:
        Metadata filter requiring owner equality.
    """

    return _owner_filter(ctx)


@auth.on.threads.update
async def authorize_thread_update(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.update.value,
) -> Auth.types.FilterType:
    """Authorize thread updates, including run cancellation, for the owner only.

    Args:
        ctx: LangGraph authentication context.
        value: Thread update payload.

    Returns:
        Metadata filter requiring owner equality.
    """

    return _owner_filter(ctx)


@auth.on.threads.create_run
async def authorize_thread_run_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create_run.value,
) -> Auth.types.FilterType:
    """Authorize run creation and enforce the requested agent policy.

    Args:
        ctx: LangGraph authentication context with policy metadata.
        value: Run creation payload to mutate with model and owner metadata.

    Returns:
        Metadata filter binding the run to the authenticated owner.

    Raises:
        Auth.exceptions.HTTPException: If the requested agent, model, or
            reasoning effort is not available to the caller.
    """

    user_metadata = ctx.user.get(METADATA_KEY, {})
    metadata = value.setdefault(METADATA_KEY, {})
    requested_model = metadata.get(METADATA_MODEL, DEFAULT_MODEL)
    if not isinstance(requested_model, str):
        _forbidden("Requested model must be a string")

    requested_reasoning = metadata.get(METADATA_REASONING_EFFORT)
    if requested_reasoning is not None and not isinstance(requested_reasoning, str):
        _forbidden("Requested reasoning effort must be a string")

    graph_id = resolve_graph_id(value)
    if graph_id is None:
        _forbidden(
            "Unsupported or missing agent policy. Pass a graph ID such as "
            "'lumi', 'forma', 'cad', 'sketch', or 'clari' as the assistant_id."
        )

    try:
        authorization = authorize_agent_run(
            graph_id=graph_id,
            has_paid_plan=bool(user_metadata.get(METADATA_HAS_ALLOWED_PLAN)),
            has_access_bypass=bool(user_metadata.get(METADATA_HAS_ACCESS_BYPASS)),
            is_vip=bool(user_metadata.get(METADATA_IS_VIP)),
            requested_model=requested_model,
            requested_reasoning_effort=requested_reasoning,
        )
    except AgentPolicyError as exc:
        _forbidden(str(exc))

    metadata[METADATA_GRAPH_ID] = authorization.graph_id
    metadata[METADATA_MODEL] = authorization.model
    if authorization.reasoning_effort is not None:
        metadata[METADATA_REASONING_EFFORT] = authorization.reasoning_effort

    return _stamp_owner(ctx, value)


def _owner_filter(ctx: Auth.types.AuthContext) -> Auth.types.FilterType:
    """Build a LangGraph metadata filter for the authenticated owner.

    Args:
        ctx: LangGraph authentication context.

    Returns:
        Metadata filter requiring owner equality.
    """

    return {METADATA_OWNER: ctx.user.identity}


def _stamp_owner(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> Auth.types.FilterType:
    """Write owner metadata into a resource payload.

    Args:
        ctx: LangGraph authentication context.
        value: Resource payload to mutate.

    Returns:
        Metadata filter requiring owner equality.
    """

    owner_filter: Auth.types.FilterType = {METADATA_OWNER: ctx.user.identity}
    metadata = value.setdefault(METADATA_KEY, {})
    metadata.update(owner_filter)
    return owner_filter


def _forbidden(detail: str) -> NoReturn:
    """Raise an authorization failure.

    Args:
        detail: Error detail returned to the caller.

    Raises:
        Auth.exceptions.HTTPException: Always raised with status 403.
    """

    raise Auth.exceptions.HTTPException(status_code=403, detail=detail)
