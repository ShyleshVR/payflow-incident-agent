import asyncio
from datetime import UTC, datetime

import httpx
from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import alertmanager
from mcp_server.tools import alerts

NOW = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)


def alert(name, severity, started, **labels):
    """An alert as Alertmanager's /api/v2/alerts returns it (the fields the tool reads, plus noise)."""
    return {
        "labels": {"alertname": name, "severity": severity, "instance": "10.1.0.7:8080", **labels},
        "annotations": {"summary": f"{name} summary", "runbook": "docs/DEPLOYMENT.md#runbooks"},
        "startsAt": started,
        "endsAt": "2026-10-11T10:05:00.000Z",
        "fingerprint": "abc123",
        "status": {"state": "active"},
    }


RAW = [
    alert("KafkaConsumerLag", "warning", "2026-10-11T09:40:00.000Z", consumergroup="ledger-service", topic="ledger-commands"),
    alert("SagaRequiresAttention", "critical", "2026-10-11T09:58:00.000Z", application="payment-service"),
    alert("OutboxBacklog", "warning", "2026-10-11T09:55:00.000Z", application="payment-service"),
]


def test_critical_alerts_come_first_then_the_longest_firing():
    result = alerts.compact(RAW, NOW)

    assert result.count == 3
    assert [a.alert for a in result.alerts] == ["SagaRequiresAttention", "KafkaConsumerLag", "OutboxBacklog"]
    assert result.alerts[1].model_dump() == {
        "alert": "KafkaConsumerLag",
        "severity": "warning",
        "about": {"consumergroup": "ledger-service", "topic": "ledger-commands"},  # no instance address
        "firing_for_minutes": 20,
        "summary": "KafkaConsumerLag summary",
    }


def test_a_flood_of_alerts_is_capped_but_still_counted():
    flood = [alert("InstanceDown", "warning", "2026-10-11T09:59:00.000Z", pod=f"pod-{i}") for i in range(50)]

    result = alerts.compact(flood, NOW)

    assert result.count == 50
    assert len(result.alerts) == alerts.MAX_ALERTS


def test_the_tool_is_served_over_mcp(monkeypatch):
    async def fake_firing_alerts():
        return RAW

    monkeypatch.setattr(alertmanager, "firing_alerts", fake_firing_alerts)
    monkeypatch.setattr(alerts, "utc_now", lambda: NOW)

    async def call():
        # connects to the server object in-process: the real MCP protocol, no network
        async with Client(mcp_server_module.server) as client:
            tools = await client.list_tools()
            result = await client.call_tool("list_active_alerts", {})
            return tools, result

    tools, result = asyncio.run(call())

    assert [t.name for t in tools.tools] == ["list_active_alerts"]
    assert "firing in PayFlow right now" in tools.tools[0].description
    assert set(tools.tools[0].output_schema["properties"]) == {"count", "alerts"}
    assert result.structured_content["count"] == 3
    assert result.structured_content["alerts"][0]["alert"] == "SagaRequiresAttention"


def test_when_alertmanager_is_down_the_tool_says_so(monkeypatch):
    async def unreachable():
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(alertmanager, "firing_alerts", unreachable)

    async def call():
        async with Client(mcp_server_module.server) as client:
            return await client.call_tool("list_active_alerts", {})

    result = asyncio.run(call())

    assert result.is_error
    assert "Alertmanager did not answer" in result.content[0].text
    assert "ConnectError" in result.content[0].text
