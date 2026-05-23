from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from texocad_agents.cad_agent.prompt import SYSTEM_PROMPT
from texocad_agents.middleware import retry_middleware
from texocad_agents.models import DEFAULT_MODEL, create_model
from texocad_agents.tools.create_cad import create_cad
from texocad_agents.tools.read_attachment import read_attachment
from texocad_agents.tools.render_view import render_view
from texocad_agents.tools.replace_file import replace_file


async def create_cad_agent(config: RunnableConfig) -> CompiledStateGraph:
    metadata = config.get("metadata", {})
    model_key = metadata.get("model", DEFAULT_MODEL)
    model = create_model(model_key)

    return create_deep_agent(
        model=model,
        tools=[read_attachment, replace_file, create_cad, render_view],
        system_prompt=SYSTEM_PROMPT,
        name="TexoCAD CAD",
        middleware=[retry_middleware],
    )
