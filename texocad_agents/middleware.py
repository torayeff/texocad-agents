import httpx
from langchain.agents.middleware import ModelRetryMiddleware

retry_middleware = ModelRetryMiddleware(
    max_retries=3,
    retry_on=(httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError),
    backoff_factor=2.0,
    initial_delay=2.0,
    on_failure="error",
)
