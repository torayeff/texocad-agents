import httpx
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from texocad_agents.models import DEFAULT_MODEL, create_model
from texocad_agents.sketch_agent.prompt import SYSTEM_PROMPT
from texocad_agents.tools.generate_sketch import generate_sketch
from texocad_agents.tools.read_attachment import read_attachment

retry_middleware = ModelRetryMiddleware(
    max_retries=3,
    retry_on=(httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError),
    backoff_factor=2.0,
    initial_delay=2.0,
    on_failure="error",
)


async def create_sketch_agent(config: RunnableConfig) -> CompiledStateGraph:
    metadata = config.get("metadata", {})
    model_key = metadata.get("model", DEFAULT_MODEL)
    model = create_model(model_key)

    return create_deep_agent(
        model=model,
        tools=[read_attachment, generate_sketch],
        system_prompt=SYSTEM_PROMPT,
        name="TexoCAD Sketch",
        middleware=[retry_middleware],
    )
