"""The one place the workflow calls tools through.

Live, a call goes to the MCP server and its result is recorded in the trace. In a replay, the
result recorded by an earlier investigation is returned instead, so the workflow can be run
again and again against one real incident without the cluster, and without causing a new one.
"""
import time
from dataclasses import dataclass
from typing import Any, Protocol

from mcp import Client

from agent.trace import Trace


@dataclass(frozen=True)
class ToolResult:
    is_error: bool
    data: Any    # the tool's structured result; for an error, its message


class Tools(Protocol):
    async def call(self, tool: str, arguments: dict | None = None) -> ToolResult: ...


class LiveTools:
    """Tools of a connected MCP server. The client must stay open while they are used."""

    def __init__(self, client: Client):
        self._client = client

    async def call(self, tool: str, arguments: dict | None = None) -> ToolResult:
        result = await self._client.call_tool(tool, arguments or {})
        if result.is_error:
            return ToolResult(True, "\n".join(block.text for block in result.content if hasattr(block, "text")))
        return ToolResult(False, result.structured_content)


class ReplayMismatch(Exception):
    """The workflow asked for a tool call the recorded investigation never made."""


class ReplayTools:
    """The tool results of a recorded investigation, each handed out once. A call is matched by
    tool name and arguments, in the order recorded: the workflow may ask in another order than
    it did then, but it cannot be given a fact that was never gathered."""

    def __init__(self, recorded_trace: dict):
        self._unused = [call for step in recorded_trace["steps"] for call in step["tool_calls"]]

    async def call(self, tool: str, arguments: dict | None = None) -> ToolResult:
        arguments = arguments or {}
        for index, call in enumerate(self._unused):
            if call["tool"] == tool and call["arguments"] == arguments:
                del self._unused[index]
                return ToolResult(call["is_error"], call["result"])
        raise ReplayMismatch(f"the recorded investigation has no (unused) call of {tool}({arguments})")


class RecordingTools:
    """Wraps live or replayed tools: every call and its result goes into the trace."""

    def __init__(self, tools: Tools, trace: Trace):
        self._tools = tools
        self._trace = trace

    async def call(self, tool: str, arguments: dict | None = None) -> ToolResult:
        arguments = arguments or {}
        clock = time.perf_counter()
        result = await self._tools.call(tool, arguments)
        self._trace.tool_call(tool, arguments, time.perf_counter() - clock, result.is_error, result.data)
        return result
