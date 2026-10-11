import asyncio

import httpx
from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import prometheus
from mcp_server.tools import health

HEALTHY = {
    "instances_up": 2.0, "instances_expected": 2.0, "uptime_seconds": 2846.6,
    "requests_per_second": 141.237, "server_errors_per_second": 0.0, "avg_response_seconds": 0.0184,
    "db_connections_waiting": 0.0, "cpu_cores_used": 1.6349,
}


def test_a_healthy_service_has_rounded_numbers_and_no_concerns():
    result = health.assess("payment-service", HEALTHY)

    assert result.model_dump() == {
        "service": "payment-service", "instances_up": 2, "instances_expected": 2,
        "minutes_since_last_start": 47, "requests_per_second": 141.24, "server_error_percent": 0.0,
        "avg_response_ms": 18, "db_connections_waiting": 0, "cpu_cores_used": 1.63, "concerns": [],
    }


def test_each_thing_that_looks_wrong_is_named():
    result = health.assess("ledger-service", HEALTHY | {
        "instances_up": 1.0, "uptime_seconds": 130.0, "server_errors_per_second": 7.06, "db_connections_waiting": 4.0,
    })

    assert result.server_error_percent == 5.0
    assert result.concerns == [
        "1 of 2 instances are down",
        "an instance started 2 minute(s) ago: a restart or a new pod",
        "5.0% of requests fail with a 5xx",
        "4 request(s) are waiting for a database connection",
    ]


def test_a_service_prometheus_cannot_see_is_reported_as_gone_not_as_healthy():
    result = health.assess("ledger-service", dict.fromkeys(HEALTHY))  # every query: no data

    assert result.instances_up == 0
    assert result.instances_expected is None
    assert result.avg_response_ms is None
    assert result.concerns == ["Prometheus sees no instance of this service: it is scaled to zero, or its pods never started"]


def test_an_idle_service_without_a_database_is_healthy():
    result = health.assess("api-gateway", HEALTHY | {
        "requests_per_second": 0.0, "server_errors_per_second": None, "avg_response_seconds": None,
        "db_connections_waiting": None,
    })

    assert (result.server_error_percent, result.avg_response_ms, result.db_connections_waiting) == (0.0, None, None)
    assert result.concerns == []


def test_all_instances_down_is_told_apart_from_some_down():
    assert health.assess("ledger-service", HEALTHY | {"instances_up": 0.0}).concerns == ["no instance is up (0 of 2)"]


def test_real_requests_are_counted_without_health_probes_and_scrapes():
    queries = health.queries("payment-service")

    assert set(queries) == set(HEALTHY)
    assert queries["instances_up"] == 'sum(up{job="payment-service"})'
    assert 'uri!~"/actuator.*"' in queries["requests_per_second"]
    assert 'outcome="SERVER_ERROR"' in queries["server_errors_per_second"]


def prometheus_answering(result):
    return httpx.AsyncClient(base_url="http://prometheus", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})))


def test_a_query_with_no_data_or_a_nan_is_none():
    async def one(result):
        async with prometheus_answering(result) as client:
            return await prometheus._one(client, "anything")

    assert asyncio.run(one([{"metric": {}, "value": [1791700000, "2"]}])) == 2.0
    assert asyncio.run(one([])) is None
    assert asyncio.run(one([{"metric": {}, "value": [1791700000, "NaN"]}])) is None


def over_mcp(arguments, monkeypatch):
    asked = []

    async def fake_instant(queries):
        asked.append(queries)
        return HEALTHY

    monkeypatch.setattr(prometheus, "instant", fake_instant)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["get_service_health"], await client.call_tool("get_service_health", arguments)

    return (*asyncio.run(call()), asked)


def test_the_tool_is_served_over_mcp_and_lists_the_services_it_accepts(monkeypatch):
    tool, result, asked = over_mcp({"service": "payment-service"}, monkeypatch)

    assert "ledger-service" in str(tool.input_schema)  # the model sees the allowed names
    assert result.structured_content["service"] == "payment-service"
    assert result.structured_content["concerns"] == []
    assert asked[0]["instances_up"] == 'sum(up{job="payment-service"})'


def test_a_service_that_does_not_exist_is_refused_before_prometheus_is_asked(monkeypatch):
    _, result, asked = over_mcp({"service": "billing-service"}, monkeypatch)

    assert result.is_error
    assert asked == []


def test_when_prometheus_is_down_the_tool_says_so(monkeypatch):
    async def unreachable(queries):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(prometheus, "instant", unreachable)

    async def call():
        async with Client(mcp_server_module.server) as client:
            return await client.call_tool("get_service_health", {"service": "payment-service"})

    result = asyncio.run(call())

    assert result.is_error
    assert "Prometheus did not answer" in result.content[0].text
