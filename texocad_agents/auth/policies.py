"""Per-agent authorization policies for LangGraph runs."""

from dataclasses import dataclass
from typing import Any

from texocad_agents.models import ALLOWED_REASONING_EFFORTS, DEFAULT_MODEL, MODELS

GRAPH_ID_CAD = "cad"
GRAPH_ID_LUMI = "lumi"
GRAPH_ID_SKETCH = "sketch"

METADATA_GRAPH_ID = "graph_id"
GRAPH_METADATA_KEYS: tuple[str, ...] = (
    METADATA_GRAPH_ID,
    "graph",
    "agent",
    "agent_id",
    "assistant_id",
)
FREE_REASONING_EFFORTS: frozenset[str] = frozenset(("none", "low"))


class AgentPolicyError(ValueError):
    """Raised when a run request violates an agent policy."""


@dataclass(frozen=True)
class AgentPolicy:
    """Access policy for one LangGraph graph."""

    graph_id: str
    allow_anonymous: bool
    requires_paid_access: bool


@dataclass(frozen=True)
class AgentRunAuthorization:
    """Normalized run settings allowed by an agent policy."""

    graph_id: str
    model: str
    reasoning_effort: str | None


AGENT_POLICIES: dict[str, AgentPolicy] = {
    GRAPH_ID_CAD: AgentPolicy(
        graph_id=GRAPH_ID_CAD,
        allow_anonymous=False,
        requires_paid_access=True,
    ),
    GRAPH_ID_LUMI: AgentPolicy(
        graph_id=GRAPH_ID_LUMI,
        allow_anonymous=True,
        requires_paid_access=False,
    ),
    GRAPH_ID_SKETCH: AgentPolicy(
        graph_id=GRAPH_ID_SKETCH,
        allow_anonymous=False,
        requires_paid_access=True,
    ),
}


def resolve_graph_id(value: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    """Resolve the requested graph ID from a LangGraph run payload.

    LangGraph clients commonly pass the graph name as `assistant_id`. If callers
    use a custom assistant UUID, they should include `metadata.graph_id` so the
    auth hook can select the correct policy.
    """

    for key in GRAPH_METADATA_KEYS:
        candidate = metadata.get(key)
        if isinstance(candidate, str) and candidate in AGENT_POLICIES:
            return candidate

    assistant_id = value.get("assistant_id")
    if isinstance(assistant_id, str) and assistant_id in AGENT_POLICIES:
        return assistant_id

    return None


def authorize_agent_run(
    *,
    graph_id: str,
    is_anonymous: bool,
    has_paid_plan: bool,
    has_access_bypass: bool,
    is_vip: bool,
    requested_model: str,
    requested_reasoning_effort: str | None,
) -> AgentRunAuthorization:
    """Authorize a run request and return normalized model settings."""

    policy = AGENT_POLICIES.get(graph_id)
    if policy is None:
        raise AgentPolicyError("Unsupported agent")

    if is_anonymous and not policy.allow_anonymous:
        raise AgentPolicyError("Sign in to use this agent")

    has_paid_access = has_paid_plan or has_access_bypass or is_vip
    if policy.requires_paid_access and not has_paid_access:
        raise AgentPolicyError("Active subscription required")

    allowed_models = set(MODELS.keys()) if is_vip else {DEFAULT_MODEL}
    if requested_model not in allowed_models:
        raise AgentPolicyError("Requested model is not available on your plan")

    allowed_reasoning_efforts = _allowed_reasoning_efforts(
        is_anonymous=is_anonymous,
        has_paid_plan=has_paid_plan,
        is_vip=is_vip,
    )
    if (
        requested_reasoning_effort is not None
        and requested_reasoning_effort not in allowed_reasoning_efforts
    ):
        raise AgentPolicyError(
            "Requested reasoning effort is not available for this agent"
        )

    return AgentRunAuthorization(
        graph_id=graph_id,
        model=requested_model,
        reasoning_effort=requested_reasoning_effort,
    )


def _allowed_reasoning_efforts(
    *,
    is_anonymous: bool,
    has_paid_plan: bool,
    is_vip: bool,
) -> set[str]:
    if is_vip or has_paid_plan:
        return set(ALLOWED_REASONING_EFFORTS)
    if is_anonymous:
        return set(FREE_REASONING_EFFORTS)
    return set(FREE_REASONING_EFFORTS)
