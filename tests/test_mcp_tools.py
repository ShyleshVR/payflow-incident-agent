import asyncio
import json

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from agent.mcp_tools import load_tools


class Lag(BaseModel):
    group: str
    messages_behind: int


def a_server() -> MCPServer:
    server = MCPServer("test")

    @server.tool()
    async def get_lag(group: str) -> Lag:
        """How far behind a consumer group is."""
        return Lag(group=group, messages_behind=42)

    @server.tool()
    async def unreachable() -> str:
        """Fails in a way the tool anticipated."""
        raise ToolError("Alertmanager did not answer")

    @server.tool()
    async def buggy() -> str:
        """Fails with a bug."""
        raise RuntimeError("password=hunter2 in the stack trace")

    return server


def run(use_tools):
    async def go():
        # in-process: the real MCP protocol, no network
        async with Client(a_server()) as client:
            return await use_tools({tool.name: tool for tool in await load_tools(client)})

    return asyncio.run(go())


def test_an_mcp_tool_becomes_a_langchain_tool_with_the_same_name_description_and_arguments():
    async def describe(tools):
        return tools["get_lag"]

    tool = run(describe)

    assert tool.name == "get_lag"
    assert tool.description == "How far behind a consumer group is."
    assert tool.args_schema["properties"]["group"]["type"] == "string"
    assert tool.args_schema["required"] == ["group"]


def test_calling_it_passes_the_arguments_and_returns_the_result_as_compact_json():
    async def call(tools):
        return await tools["get_lag"].ainvoke({"group": "ledger-service"})

    result = run(call)

    assert result == '{"group":"ledger-service","messages_behind":42}'
    assert json.loads(result)["messages_behind"] == 42


def test_an_anticipated_failure_reaches_the_model_as_text_instead_of_crashing_the_run():
    async def call(tools):
        return await tools["unreachable"].ainvoke({})

    assert "Alertmanager did not answer" in run(call)


def test_an_unexpected_failure_does_not_crash_the_run_or_leak_its_details():
    async def call(tools):
        return await tools["buggy"].ainvoke({})

    result = run(call)

    assert "Error executing tool buggy" in result
    assert "hunter2" not in result
