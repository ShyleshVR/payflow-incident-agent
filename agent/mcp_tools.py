"""MCP tools as LangChain tools.

The MCP server describes each tool (name, description, argument schema); LangChain needs the
same three things plus a function to call. This is the whole bridge. (langchain-mcp-adapters does
the same job but still requires version 1 of the mcp package; we are on version 2.)
"""
import json

from langchain_core.tools import StructuredTool, ToolException
from mcp import Client
from mcp.types import Tool


async def load_tools(client: Client) -> list[StructuredTool]:
    """Every tool of the connected MCP server. The client must stay open while they are used."""
    listed = await client.list_tools()
    return [_as_langchain_tool(client, tool) for tool in listed.tools]


def _as_langchain_tool(client: Client, tool: Tool) -> StructuredTool:
    async def call(**arguments) -> str:
        result = await client.call_tool(tool.name, arguments)
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        if result.is_error:
            # LangChain turns this into a tool message the model can read and react to
            raise ToolException(text or f"{tool.name} failed")
        # the model reads text: compact JSON when the tool returned structured data
        if result.structured_content is not None:
            return json.dumps(result.structured_content, separators=(",", ":"))
        return text

    return StructuredTool(
        name=tool.name,
        description=tool.description or "",
        args_schema=tool.input_schema,
        coroutine=call,
        handle_tool_error=True,
    )
