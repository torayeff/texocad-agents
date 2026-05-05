import os

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langchain_anthropic import ChatAnthropic
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from texocad_agents.design_agent.prompt import SYSTEM_PROMPT
from texocad_agents.models import (
    DEFAULT_MODEL,
    MODELS,
    with_google_vertex_credentials,
)
from texocad_agents.tools.build_design import build_design
from texocad_agents.tools.file_system import replace_file
from texocad_agents.tools.read_attachment import read_attachment


async def create_design_agent(config: RunnableConfig) -> CompiledStateGraph:
    metadata = config.get("metadata", {})
    model_key = metadata.get("model", DEFAULT_MODEL)
    if model_key not in MODELS:
        raise ValueError(f"Unknown model key: {model_key}")

    model_config = MODELS[model_key]
    if model_config.provider == "azure_anthropic":
        api_key = os.environ["ANTHROPIC_FOUNDRY_API_KEY"]
        model = ChatAnthropic(
            model=model_config.name,
            api_key=api_key,
            base_url=os.environ["ANTHROPIC_FOUNDRY_BASE_URL"],
            default_headers={"api-key": api_key},
            **model_config.kwargs,
        )
    else:
        model = init_chat_model(
            model_config.name,
            model_provider=model_config.provider,
            **with_google_vertex_credentials(model_config.kwargs),
        )

    return create_deep_agent(
        model=model,
        tools=[build_design, read_attachment, replace_file],
        system_prompt=SYSTEM_PROMPT,
        name="design-agent",
    )
