"""Spike: the whole chain. The local model decides to call a tool, LangChain passes the call to
the MCP server, the MCP server asks the live PayFlow cluster, and the model reads the answer.

Needs: the model server (scripts/start-llm.ps1), the MCP server (python -m mcp_server.server)
and the PayFlow cluster. Run:
    .venv\\Scripts\\python.exe spike\\02_model_calls_mcp_tool.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from mcp import Client

from agent.mcp_tools import load_tools

MCP_URL = "http://127.0.0.1:8091/mcp"

llm = ChatOpenAI(
    base_url="http://127.0.0.1:8090/v1",
    api_key="not-needed",
    model="qwen3.5-9b",
    temperature=0,
    max_tokens=600,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


async def main() -> None:
    async with Client(MCP_URL) as mcp:
        tools = await load_tools(mcp)
        print("tools from the MCP server:", [tool.name for tool in tools])
        by_name = {tool.name: tool for tool in tools}
        llm_with_tools = llm.bind_tools(tools)

        messages = [
            SystemMessage("You are the on-call engineer for PayFlow, a payments system. Get the facts "
                          "with the tools before answering. Answer in two or three plain sentences."),
            HumanMessage("Is anything wrong in PayFlow right now?"),
        ]

        started = time.perf_counter()
        first = await llm_with_tools.ainvoke(messages)
        print("model asked for:          ", [(c["name"], c["args"]) for c in first.tool_calls])
        messages.append(first)

        for call in first.tool_calls:
            # model -> LangChain tool -> MCP -> Alertmanager in the cluster
            result = await by_name[call["name"]].ainvoke(call["args"])
            print("MCP server returned:      ", result)
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))

        final = await llm_with_tools.ainvoke(messages)
        print("model answered:           ", final.content.strip())
        print(f"({time.perf_counter() - started:.1f}s end to end)")


if __name__ == "__main__":
    asyncio.run(main())
