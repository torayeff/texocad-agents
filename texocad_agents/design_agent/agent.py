from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from texocad_agents.design_agent.prompt import SYSTEM_PROMPT
from texocad_agents.models import DEFAULT_MODEL, create_model
from texocad_agents.tools.build_design import build_design
from texocad_agents.tools.file_system import replace_file
from texocad_agents.tools.read_attachment import read_attachment


async def create_design_agent(config: RunnableConfig) -> CompiledStateGraph:
    metadata = config.get("metadata", {})
    model_key = metadata.get("model", DEFAULT_MODEL)
    model = create_model(model_key)

    return create_deep_agent(
        model=model,
        tools=[build_design, read_attachment, replace_file],
        system_prompt=SYSTEM_PROMPT,
        name="design-agent",
    )
