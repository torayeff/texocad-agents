"""Define the factory for constructing the TexoCAD sketch agent."""

from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from texocad_agents.models import create_model
from texocad_agents.sketch_agent.prompt import SYSTEM_PROMPT
from texocad_agents.tools.create_sketch import create_sketch
from texocad_agents.tools.read_attachment import read_attachment


async def create_sketch_agent(config: RunnableConfig) -> CompiledStateGraph:
    """Create a configured sketch deep agent.

    Args:
        config: LangGraph runnable configuration. `metadata.model` may select a
            model key from the TexoCAD model registry.

    Returns:
        Compiled deep agent graph with attachment reading and sketch creation
        tools.
    """

    model = create_model(config.get("metadata", {}).get("model"))

    return create_deep_agent(
        model=model,
        tools=[read_attachment, create_sketch],
        system_prompt=SYSTEM_PROMPT,
        name="TexoCAD Sketch",
    )
