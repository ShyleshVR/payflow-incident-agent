import asyncio

import httpx
from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import prometheus
from mcp_server.tools import dependencies


def one(value):
    return [({}, float(value))]


def calls(*rows):
    """(operation, outcome, count) rows, as Prometheus groups them."""
    return [({"operation": operation, "outcome": outcome}, float(count)) for operation, outcome, count in rows]


def seconds(**per_operation):
    return [({"operation": operation}, float(value)) for operation, value in per_operation.items()]


PROCESSOR_CALLS = calls(("authorize", "SUCCEEDED", 100), ("authorize", "DECLINED", 10), ("capture", "SUCCEEDED", 100))
BANK_CALLS = calls(("transfer", "OK", 2), ("transfer", "DECLINED", 2), ("status", "OK", 15.02))

# What Prometheus answers when both partners work (the shapes are the live ones).
WORKING = {
    "breaker_states": [({"state": "closed"}, 2.0), ({"state": "open"}, 0.0), ({"state": "half_open"}, 0.0)],
    "breaker_opened": one(0), "breaker_refused": one(0), "breaker_failure_rate": one(-1),
    "processor_caller_up": one(2), "processor_watched_before": one(2),
    "processor_calls": PROCESSOR_CALLS, "processor_calls_new": [], "processor_calls_plain": PROCESSOR_CALLS,
    "processor_seconds": seconds(authorize=5.5, capture=3.0),
    "bank_caller_up": one(2), "bank_watched_before": one(2),
    "bank_calls": BANK_CALLS, "bank_calls_new": [], "bank_calls_plain": BANK_CALLS,
    "bank_seconds": seconds(transfer=0.2, status=0.3),
}


def test_working_partners_have_totals_a_row_per_operation_and_no_concerns():
    result = dependencies.assess(15, WORKING)

    assert result.model_dump() == {
        "minutes": 15,
        "processor": {
            "called_by": "payment-service", "calls": 210, "succeeded": 200, "declined": 10, "rejected": 0,
            "unanswered": 0, "unanswered_percent": 0.0, "avg_ms": 40,
            "operations": [
                {"operation": "authorize", "calls": 110, "succeeded": 100, "declined": 10, "rejected": 0, "unanswered": 0, "avg_ms": 50},
                {"operation": "capture", "calls": 100, "succeeded": 100, "declined": 0, "rejected": 0, "unanswered": 0, "avg_ms": 30},
            ],
        },
        "processor_circuit_breaker": {"state": "closed", "instances": 2, "instances_not_closed": 0,
                                      "opened_in_window": False, "calls_refused": 0, "recent_failure_percent": None},
        "bank": {
            "called_by": "payout-service", "calls": 19, "succeeded": 17, "declined": 2, "rejected": 0,
            "unanswered": 0, "unanswered_percent": 0.0, "avg_ms": 26,
            "operations": [
                {"operation": "status", "calls": 15, "succeeded": 15, "declined": 0, "rejected": 0, "unanswered": 0, "avg_ms": 20},
                {"operation": "transfer", "calls": 4, "succeeded": 2, "declined": 2, "rejected": 0, "unanswered": 0, "avg_ms": 50},
            ],
        },
        # half the transfers were declined, and that is the bank working
        "concerns": [],
    }


def test_an_open_breaker_is_the_worst_state_of_any_instance_and_says_what_it_refused():
    result = dependencies.assess(15, WORKING | {
        "breaker_states": [({"state": "closed"}, 1.0), ({"state": "open"}, 1.0), ({"state": "half_open"}, 0.0)],
        "breaker_opened": one(1), "breaker_refused": one(37.4), "breaker_failure_rate": one(85.0),
    })

    assert result.processor_circuit_breaker.model_dump() == {
        "state": "open", "instances": 2, "instances_not_closed": 1, "opened_in_window": True,
        "calls_refused": 37, "recent_failure_percent": 85}
    assert result.concerns == [
        ("the processor circuit breaker is open on 1 of 2 payment-service instance(s): calls to the processor are "
        "refused without being sent; it refused 37 call(s) in the last 15 minutes")]


def test_a_half_open_breaker_and_one_that_opened_earlier_are_told_apart():
    half_open = dependencies.assess(15, WORKING | {
        "breaker_states": [({"state": "closed"}, 0.0), ({"state": "half_open"}, 2.0)], "breaker_opened": one(1)})
    earlier = dependencies.assess(60, WORKING | {"breaker_opened": one(1), "breaker_refused": one(12)})
    # open lasts 10 seconds: no scrape caught it, but the calls it refused were counted
    between_scrapes = dependencies.assess(60, WORKING | {"breaker_refused": one(4)})

    assert half_open.concerns == [
        ("the processor circuit breaker is half-open on 2 of 2 payment-service instance(s): trial calls are testing "
        "whether the processor is back")]
    assert earlier.processor_circuit_breaker.state == "closed"
    assert earlier.concerns == [
        ("the processor circuit breaker opened during the last 60 minutes and is closed again now; it refused 12 "
        "call(s) in the last 60 minutes")]
    assert between_scrapes.processor_circuit_breaker.opened_in_window is True
    assert between_scrapes.concerns[0].startswith("the processor circuit breaker opened during the last 60 minutes")


def test_unanswered_calls_are_a_concern_from_one_percent_with_the_operations_they_hit():
    def assessed(unanswered_captures):
        rows = calls(("authorize", "SUCCEEDED", 100), ("authorize", "UNKNOWN", 40), ("capture", "SUCCEEDED", 60),
                     ("capture", "UNKNOWN", unanswered_captures))
        return dependencies.assess(15, WORKING | {"processor_calls": rows, "processor_calls_plain": rows})

    outage = assessed(12)
    assert (outage.processor.calls, outage.processor.unanswered, outage.processor.unanswered_percent) == (212, 52, 24.5)
    assert outage.concerns == [
        "52 of 212 calls to the processor (24.5%) got no answer to trust in the last 15 minutes (authorize 40, capture 12)"]

    def share(unanswered, answered):
        rows = calls(("transfer", "OK", answered), ("transfer", "UNKNOWN", unanswered))
        return dependencies.assess(15, WORKING | {"bank_calls": rows, "bank_calls_plain": rows})

    assert share(1, 99).concerns == ["1 of 100 calls to the bank (1%) got no answer to trust in the last 15 minutes (transfer 1)"]
    assert share(1, 999).concerns == []   # 0.1%: a single timeout among a thousand calls
    assert share(1, 2).concerns == ["1 of 3 calls to the bank (33.3%) got no answer to trust in the last 15 minutes (transfer 1)"]


def test_a_rejected_request_is_a_concern_and_a_decline_never_is():
    rejected = calls(("transfer", "OK", 5), ("transfer", "REJECTED", 2), ("status", "REJECTED", 1))
    all_declined = calls(("authorize", "DECLINED", 50))

    result = dependencies.assess(15, WORKING | {
        "bank_calls": rejected, "bank_calls_plain": rejected,
        "processor_calls": all_declined, "processor_calls_plain": all_declined,
    })

    assert (result.processor.declined, result.processor.succeeded) == (50, 0)
    assert result.concerns == [
        "the bank rejected 3 request(s) as wrong (status 1, transfer 2): a bug or a misconfiguration on PayFlow's side"]


def test_failures_whose_counter_first_appeared_in_the_window_are_counted():
    # The outage's first unanswered calls created the UNKNOWN series: it did not exist when the
    # window began, so its rise is invisible, and its whole value happened inside the window.
    result = dependencies.assess(15, WORKING | {
        "processor_calls": calls(("authorize", "SUCCEEDED", 100)),
        "processor_calls_new": calls(("authorize", "UNKNOWN", 6), ("refund", "REJECTED", 3)),
        "processor_calls_plain": calls(("authorize", "SUCCEEDED", 100), ("authorize", "UNKNOWN", 2)),
    })

    assert (result.processor.calls, result.processor.unanswered, result.processor.rejected) == (109, 6, 3)
    assert len(result.concerns) == 2


def test_without_history_only_the_rise_inside_the_window_is_counted():
    # Prometheus restarted: nothing is known about the window's start, every series looks new,
    # and counting their whole values would report days of calls as the last 15 minutes
    result = dependencies.assess(15, WORKING | {
        "processor_watched_before": [],
        "processor_calls": [],
        "processor_calls_new": calls(("authorize", "SUCCEEDED", 500000), ("authorize", "UNKNOWN", 9000)),
        "processor_calls_plain": calls(("authorize", "SUCCEEDED", 40)),
    })

    assert (result.processor.calls, result.processor.unanswered) == (40, 0)
    assert result.concerns == []


def test_a_partner_nobody_called_has_zero_calls_and_is_not_called_healthy_or_broken():
    result = dependencies.assess(15, WORKING | {
        "processor_calls": [], "processor_calls_plain": [], "processor_seconds": [],
        "bank_calls": [], "bank_calls_plain": [], "bank_seconds": [],
    })

    assert result.bank.model_dump() == {
        "called_by": "payout-service", "calls": 0, "succeeded": 0, "declined": 0, "rejected": 0, "unanswered": 0,
        "unanswered_percent": 0.0, "avg_ms": None, "operations": []}
    assert result.processor.calls == 0
    assert result.concerns == []


def test_a_caller_that_is_down_is_named_once():
    down = dependencies.assess(15, WORKING | {
        "breaker_states": [], "breaker_opened": [], "breaker_refused": [], "breaker_failure_rate": [],
        "processor_caller_up": one(0), "processor_calls": [], "processor_calls_plain": [], "processor_seconds": [],
        "bank_caller_up": [], "bank_calls": [], "bank_calls_plain": [], "bank_seconds": [],
    })
    no_breaker = dependencies.assess(15, WORKING | {"breaker_states": []})

    assert down.processor_circuit_breaker is None
    assert down.concerns == [
        "payment-service, the only caller of the processor, has no instance up: nothing can reach the processor",
        "payout-service, the only caller of the bank, has no instance up: nothing can reach the bank",
    ]
    assert no_breaker.concerns == ["payment-service is up but reports no circuit breaker for the processor"]


def test_an_outcome_this_code_does_not_know_is_not_counted_as_an_answer():
    rows = calls(("transfer", "OK", 9), ("transfer", "SOMETHING_NEW", 1))

    result = dependencies.assess(15, WORKING | {"bank_calls": rows, "bank_calls_plain": rows})

    assert (result.bank.succeeded, result.bank.unanswered) == (9, 1)


def test_calls_are_counted_per_partner_from_its_callers_timer_over_the_window():
    queries = dependencies.queries(45)

    assert set(queries) == set(WORKING)
    assert queries["breaker_states"] == 'sum by (state) (resilience4j_circuitbreaker_state{name="processor"})'
    assert "[45m]" in queries["breaker_opened"] and "open|forced_open|half_open" in queries["breaker_opened"]
    assert queries["processor_calls"] == (
        'sum by (operation, outcome) (increase(processor_calls_seconds_count{job="payment-service"}[45m]) '
        'and processor_calls_seconds_count{job="payment-service"} offset 45m)')
    assert queries["bank_calls_new"] == (
        'sum by (operation, outcome) (bank_calls_seconds_count{job="payout-service"} '
        'unless bank_calls_seconds_count{job="payout-service"} offset 45m)')
    assert queries["bank_seconds"] == 'sum by (operation) (increase(bank_calls_seconds_sum{job="payout-service"}[45m]))'
    assert queries["bank_watched_before"] == 'count(up{job="payout-service"} offset 45m)'


def over_mcp(arguments, monkeypatch):
    asked = []

    async def fake_vectors(queries):
        asked.append(queries)
        return WORKING

    monkeypatch.setattr(prometheus, "vectors", fake_vectors)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["get_dependency_status"], await client.call_tool("get_dependency_status", arguments)

    return (*asyncio.run(call()), asked)


def test_the_tool_is_served_over_mcp_and_needs_no_arguments(monkeypatch):
    tool, result, asked = over_mcp({}, monkeypatch)

    assert tool.input_schema.get("required", []) == []
    assert result.structured_content["minutes"] == 15
    assert result.structured_content["processor"]["calls"] == 210
    assert result.structured_content["processor_circuit_breaker"]["state"] == "closed"
    assert result.structured_content["concerns"] == []
    assert "[15m]" in asked[0]["processor_calls"]


def test_a_window_out_of_range_is_brought_into_range_not_refused(monkeypatch):
    _, result, asked = over_mcp({"minutes": 0}, monkeypatch)

    assert not result.is_error
    assert result.structured_content["minutes"] == 1
    assert "[1m]" in asked[0]["bank_calls"]


def test_prometheus_not_answering_is_an_error_the_model_can_read(monkeypatch):
    async def unreachable(queries):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(prometheus, "vectors", unreachable)

    async def call():
        async with Client(mcp_server_module.server) as client:
            return await client.call_tool("get_dependency_status", {})

    result = asyncio.run(call())

    assert result.is_error
    assert "Prometheus did not answer" in result.content[0].text
