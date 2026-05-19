from deepagents.backends.utils import create_file_data
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from texocad_agents.tools.tool_runtime import normalize_path


@tool(parse_docstring=True)
def replace_file(file_path: str, content: str, runtime: ToolRuntime) -> Command:
    """Replace a file in the agent filesystem.

    Use this instead of edit_file when rewriting a complete CAD script or when
    a change touches multiple dependent statements.

    Args:
        file_path: Path to the file, for example "/models/bracket.py".
        content: Complete replacement file content.
    """

    resolved_path = normalize_path(file_path)
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
