"""The MCP server: PayFlow's facts as tools for the incident agent.

Run:  .venv\\Scripts\\python.exe -m mcp_server.server     (http://127.0.0.1:8091/mcp)
"""
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import config
from mcp_server.clients import alertmanager
from mcp_server.tools import alerts

server = MCPServer(
    "payflow",
    instructions="Read-only facts about the PayFlow payments system, for investigating its alerts.",
)


# The docstring is the tool's description: it is what the model reads to decide when to call it.
@server.tool()
async def list_active_alerts() -> alerts.ActiveAlerts:
    """Lists the alerts firing in PayFlow right now: name, severity, what each is about, how long
    it has been firing and its one-line summary. Critical alerts come first. Call this first in
    any investigation, and again later to see whether an alert has cleared."""
    try:
        raw = await alertmanager.firing_alerts()
    except httpx.HTTPError as e:
        # A ToolError's message reaches the model; any other exception's text is hidden from clients.
        raise ToolError(f"Alertmanager did not answer at {config.ALERTMANAGER_URL} ({type(e).__name__}). "
                        "The alert list is unavailable; this says nothing about PayFlow itself.") from e
    return alerts.compact(raw, alerts.utc_now())


if __name__ == "__main__":
    server.run(transport="streamable-http", host=config.MCP_HOST, port=config.MCP_PORT)
