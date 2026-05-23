"""Define the factory for constructing the TexoCAD CAD agent."""

from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from texocad_agents.cad_agent.prompt import SYSTEM_PROMPT
from texocad_agents.models import create_model
from texocad_agents.tools.create_cad import create_cad
from texocad_agents.tools.read_attachment import read_attachment
from texocad_agents.tools.render_view import render_view
from texocad_agents.tools.replace_file import replace_file


async def create_cad_agent(config: RunnableConfig) -> CompiledStateGraph:
    """Create a configured CAD deep agent.

    Args:
        config: LangGraph runnable configuration. `metadata.model` may select a
            model key from the TexoCAD model registry.

    Returns:
        Compiled deep agent graph with attachment reading, file replacement,
        CAD creation, and render-view tools.
    """

    model = create_model(config.get("metadata", {}).get("model"))

    return create_deep_agent(
        model=model,
        tools=[read_attachment, replace_file, create_cad, render_view],
        system_prompt=SYSTEM_PROMPT,
        name="CAD Agent",
    )
