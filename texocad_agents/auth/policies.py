"""Per-agent authorization policies for LangGraph runs."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from langgraph_api.graph import NAMESPACE_GRAPH

from texocad_agents.models import ALLOWED_REASONING_EFFORTS, DEFAULT_MODEL, MODELS

GRAPH_ID_CAD = "cad"
GRAPH_ID_CLARI = "clari"
GRAPH_ID_FORMA = "forma"
GRAPH_ID_LUMI = "lumi"
GRAPH_ID_SKETCH = "sketch"

METADATA_GRAPH_ID = "graph_id"
FREE_REASONING_EFFORTS: frozenset[str] = frozenset(("none", "low"))


class AgentPolicyError(ValueError):
    """Raised when a run request violates an agent policy."""


@dataclass(frozen=True)
class AgentPolicy:
    """Access policy for one LangGraph graph."""

    graph_id: str
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
        requires_paid_access=True,
    ),
    GRAPH_ID_CLARI: AgentPolicy(
        graph_id=GRAPH_ID_CLARI,
        requires_paid_access=True,
    ),
    GRAPH_ID_FORMA: AgentPolicy(
        graph_id=GRAPH_ID_FORMA,
        requires_paid_access=True,
    ),
    GRAPH_ID_LUMI: AgentPolicy(
        graph_id=GRAPH_ID_LUMI,
        requires_paid_access=False,
    ),
    GRAPH_ID_SKETCH: AgentPolicy(
        graph_id=GRAPH_ID_SKETCH,
        requires_paid_access=True,
    ),
}


# LangGraph rewrites a graph name into a deterministic assistant UUID
# (uuid5(NAMESPACE_GRAPH, graph_id)) before the auth handler runs, so the
# run payload arrives keyed by that UUID rather than the graph name.
GRAPH_ID_BY_ASSISTANT_UUID: dict[str, str] = {
    str(uuid5(NAMESPACE_GRAPH, graph_id)): graph_id for graph_id in AGENT_POLICIES
}


def resolve_graph_id(value: dict[str, Any]) -> str | None:
    """Resolve the requested graph ID from the LangGraph run payload."""

    assistant_id = value.get("assistant_id")
    if isinstance(assistant_id, UUID):
        assistant_id = str(assistant_id)
    if not isinstance(assistant_id, str):
        return None
    if assistant_id in AGENT_POLICIES:
        return assistant_id
    return GRAPH_ID_BY_ASSISTANT_UUID.get(assistant_id)


def authorize_agent_run(
    *,
    graph_id: str,
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

    has_paid_access = has_paid_plan or has_access_bypass or is_vip
    if policy.requires_paid_access and not has_paid_access:
        raise AgentPolicyError("Active subscription required")

    allowed_models = set(MODELS.keys()) if is_vip else {DEFAULT_MODEL}
    if requested_model not in allowed_models:
        raise AgentPolicyError("Requested model is not available on your plan")

    allowed_reasoning_efforts = (
        set(ALLOWED_REASONING_EFFORTS)
        if is_vip or has_paid_plan
        else set(FREE_REASONING_EFFORTS)
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
