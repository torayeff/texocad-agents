"""Replace files in the agent filesystem through LangGraph state updates."""

from deepagents.backends.utils import create_file_data
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command


@tool(parse_docstring=True)
def replace_file(file_path: str, content: str, runtime: ToolRuntime) -> Command:
    """Replace a file in the agent filesystem.

    Use this instead of edit_file when rewriting a complete CAD script or when
    a change touches multiple dependent statements.

    Args:
        file_path: Path to the file, for example "/models/bracket.py".
        content: Complete replacement file content.
        runtime: LangGraph tool runtime for the current tool call.

    Returns:
        Command that writes the replacement file content into agent state and
        appends a confirmation tool message.
    """

    resolved_path = file_path if file_path.startswith("/") else f"/{file_path}"
    return Command(
        update={
            "files": {resolved_path: create_file_data(content)},
            "messages": [
                ToolMessage(
                    content=f"Replaced {resolved_path}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )
