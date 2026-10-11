"""Prometheus' HTTP API: instant queries."""
import asyncio
import math

import httpx

from mcp_server import config


async def instant(queries: dict[str, str]) -> dict[str, float | None]:
    """Runs the named queries concurrently. Each must reduce to a single number (sum, max, ...);
    a query with no data gives None, which is itself a fact (e.g. a service with no instances)."""
    async with httpx.AsyncClient(base_url=config.PROMETHEUS_URL, timeout=5) as client:
        values = await asyncio.gather(*(_one(client, promql) for promql in queries.values()))
    return dict(zip(queries, values, strict=True))


async def _one(client: httpx.AsyncClient, promql: str) -> float | None:
    response = await client.get("/api/v1/query", params={"query": promql})
    response.raise_for_status()
    result = response.json()["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else value  # 0 / 0 on an idle service is "no data" too
