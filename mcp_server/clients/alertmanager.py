"""Alertmanager's HTTP API (v2)."""
import httpx

from mcp_server import config


async def firing_alerts() -> list[dict]:
    """The alerts firing now, as Alertmanager returns them. Silenced and inhibited ones are left
    out: Alertmanager isn't notifying anyone about those either."""
    async with httpx.AsyncClient(base_url=config.ALERTMANAGER_URL, timeout=5) as client:
        response = await client.get("/api/v2/alerts",
                                    params={"active": "true", "silenced": "false", "inhibited": "false"})
        response.raise_for_status()
        return response.json()
