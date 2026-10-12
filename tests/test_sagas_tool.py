import asyncio
from datetime import UTC, datetime

import httpx
from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import payflow
from mcp_server.tools import sagas

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
PAY = "pay_8e29813f-a522-45e0-a8f6-32e22bf66e85"
PAYOUT = "po_3d5f6a10-1b7c-4c58-9f0a-2f6f1c1f7a01"


def payment_item(payment_id, reason, state, stuck_state=None, minutes=42, saga_type="PAYMENT", error="No answer to the capture"):
    return {"paymentId": payment_id, "sagaId": "4b2d7c1e-0000-4000-8000-000000000001", "type": saga_type, "reason": reason,
            "state": state, "stuckState": stuck_state, "attempt": 4, "stepStartedAt": "2026-10-12T08:18:00",
            "minutesInStep": minutes, "failureCode": "processor_unavailable", "lastError": error,
            "createdAt": "2026-10-12T08:17:00"}


# PayFlow's two answers, in the shapes the endpoints return.
PAYMENTS = {"parked": 1, "stuck": 1, "stuckAfterMinutes": 15, "sagas": [
    payment_item(PAY, "PARKED", "REQUIRES_ATTENTION", "CAPTURING", minutes=42),
    payment_item("pay_11111111-1111-4111-8111-111111111111", "STUCK", "SETTLING", minutes=20, error=None),
]}
PAYOUTS = {"parked": 1, "stuck": 0, "stuckAfterMinutes": 15, "sagas": [
    {"payoutId": PAYOUT, "sagaId": "4b2d7c1e-0000-4000-8000-000000000002", "reason": "PARKED", "state": "REQUIRES_ATTENTION",
     "stuckState": "SUBMITTING", "attempt": 6, "stepStartedAt": "2026-10-12T07:00:00", "minutesInStep": 120,
     "failureCode": "bank_unavailable", "lastError": "unreachable ConnectException", "createdAt": "2026-10-12T06:59:00"},
]}
NOTHING = {"parked": 0, "stuck": 0, "stuckAfterMinutes": 15, "sagas": []}


def test_parked_and_stuck_sagas_of_both_services_are_one_list_parked_first():
    result = sagas.summarise(PAYMENTS, PAYOUTS)

    assert result.model_dump() == {
        "stuck_after_minutes": 15, "payments_parked": 1, "payments_stuck": 1, "payouts_parked": 1, "payouts_stuck": 0,
        "sagas": [
            {"id": PAYOUT, "kind": "payout", "reason": "parked", "stopped_in": "SUBMITTING", "minutes": 120, "attempts": 6,
             "failure_code": "bank_unavailable", "last_error": "unreachable ConnectException"},
            {"id": PAY, "kind": "payment", "reason": "parked", "stopped_in": "CAPTURING", "minutes": 42, "attempts": 4,
             "failure_code": "processor_unavailable", "last_error": "No answer to the capture"},
            {"id": "pay_11111111-1111-4111-8111-111111111111", "kind": "payment", "reason": "stuck", "stopped_in": "SETTLING",
             "minutes": 20, "attempts": 4, "failure_code": "processor_unavailable", "last_error": None},
        ],
        "not_listed": 0,
        "concerns": [
            f"1 payment saga(s) are parked and wait for an operator's retry; the oldest, {PAY}, for 42 minutes in CAPTURING",
            ("1 payment saga(s) have been in the same step for over 15 minutes; the oldest, "
            "pay_11111111-1111-4111-8111-111111111111, for 20 minutes in SETTLING"),
            f"1 payout saga(s) are parked and wait for an operator's retry; the oldest, {PAYOUT}, for 120 minutes in SUBMITTING",
        ],
    }


def test_nothing_needing_attention_is_an_empty_list_without_concerns():
    result = sagas.summarise(NOTHING, NOTHING)

    assert (result.sagas, result.not_listed, result.concerns) == ([], 0, [])
    assert (result.payments_parked, result.payouts_stuck) == (0, 0)


def test_a_refund_saga_is_called_a_refund_and_long_errors_are_shortened():
    long_error = "java.lang.IllegalStateException: " + "x" * 400
    result = sagas.summarise({"parked": 1, "stuck": 0, "sagas": [
        payment_item(PAY, "PARKED", "REQUIRES_ATTENTION", "REFUNDING", saga_type="REFUND", error=long_error)]}, NOTHING)

    assert result.sagas[0].kind == "refund"
    assert len(result.sagas[0].last_error) == sagas.MAX_TEXT
    assert result.sagas[0].last_error.endswith("…")


def test_more_sagas_than_listed_are_counted_as_not_listed():
    many = {"parked": 240, "stuck": 3, "sagas": PAYMENTS["sagas"]}

    result = sagas.summarise(many, NOTHING)

    assert result.payments_parked == 240
    assert result.not_listed == 241
    assert result.concerns[0].startswith("240 payment saga(s) are parked")


def test_a_service_that_did_not_answer_is_unknown_and_the_other_is_still_reported():
    result = sagas.summarise(None, PAYOUTS)

    assert (result.payments_parked, result.payments_stuck, result.payouts_parked) == (None, None, 1)
    assert [saga.id for saga in result.sagas] == [PAYOUT]
    assert result.concerns[0] == "payment-service did not answer: whether its sagas need attention is unknown"
    assert len(result.concerns) == 2


# ---- one saga

def step(state, outcome, at, detail=None):
    return {"state": state, "outcome": outcome, "detail": detail, "occurredAt": at}


PARKED_PAYMENT = [{
    "sagaId": "4b2d7c1e-0000-4000-8000-000000000001", "type": "PAYMENT", "state": "REQUIRES_ATTENTION",
    "stuckState": "CAPTURING", "attempt": 4, "nextAttemptAt": None, "stepStartedAt": "2026-10-12T08:18:00",
    "failureCode": "processor_unavailable", "lastError": "Capture outcome unknown after 10m", "createdAt": "2026-10-12T08:07:00",
    "finishedAt": None,
    "steps": [step("AUTHORIZING", "AUTHORIZED", "2026-10-12T08:07:01"),
              step("CAPTURING", "UNKNOWN", "2026-10-12T08:07:02", "503 Service Unavailable"),
              step("CAPTURING", "PARKED", "2026-10-12T08:18:00", "Capture outcome unknown after 10m")],
}]
LOOKED_UP = [{"paymentId": PAY, "status": "PROCESSING", "amount": 40.00, "currency": "USD", "sagaActive": True}]


def test_a_parked_saga_shows_where_it_stopped_why_its_steps_and_the_payments_status():
    result = sagas.detail(PAY, PARKED_PAYMENT, LOOKED_UP, NOW)

    assert result.model_dump() == {
        "id": PAY, "kind": "payment", "status": "PROCESSING", "amount": "40.00 USD", "state": "REQUIRES_ATTENTION",
        "stopped_in": "CAPTURING", "finished": False, "attempts": 4, "minutes_since_last_step": 42,
        "failure_code": "processor_unavailable", "last_error": "Capture outcome unknown after 10m",
        "steps": [
            {"step": "AUTHORIZING", "outcome": "AUTHORIZED", "detail": None, "times": 1, "minutes_ago": 52},
            {"step": "CAPTURING", "outcome": "UNKNOWN", "detail": "503 Service Unavailable", "times": 1, "minutes_ago": 52},
            {"step": "CAPTURING", "outcome": "PARKED", "detail": "Capture outcome unknown after 10m", "times": 1, "minutes_ago": 42},
        ],
        "earlier_steps": 0, "other_sagas": 0,
        "concerns": ["parked in CAPTURING and waiting for an operator's retry: Capture outcome unknown after 10m"],
    }


def test_of_a_payments_sagas_the_unfinished_one_is_shown():
    finished_payment = PARKED_PAYMENT[0] | {"state": "COMPLETED", "stuckState": None, "finishedAt": "2026-10-12T08:07:05",
                                            "createdAt": "2026-10-12T08:07:00", "lastError": None}
    running_refund = PARKED_PAYMENT[0] | {"type": "REFUND", "state": "REFUNDING", "stuckState": None, "lastError": None,
                                          "createdAt": "2026-10-12T08:30:00",
                                          "steps": [step("HOLDING", "HELD", "2026-10-12T08:30:01")]}

    result = sagas.detail(PAY, [finished_payment, running_refund], LOOKED_UP, NOW)

    assert (result.kind, result.state, result.finished, result.other_sagas) == ("refund", "REFUNDING", False, 1)
    assert result.concerns == ["still running, but no step outcome for 29 minutes (in REFUNDING)"]


def test_a_finished_saga_and_one_that_just_moved_have_no_concerns():
    finished = PARKED_PAYMENT[0] | {"state": "COMPLETED", "stuckState": None, "finishedAt": "2026-10-12T08:07:05"}
    moving = PARKED_PAYMENT[0] | {"state": "SETTLING", "stuckState": None,
                                  "steps": [step("CAPTURING", "CAPTURED", "2026-10-12T08:59:30")]}

    assert sagas.detail(PAY, [finished], LOOKED_UP, NOW).concerns == []
    assert sagas.detail(PAY, [moving], LOOKED_UP, NOW).concerns == []


def test_only_the_last_steps_are_shown_and_the_rest_counted():
    many = PARKED_PAYMENT[0] | {"steps": [step("CAPTURING", "UNKNOWN", f"2026-10-12T08:{minute:02d}:00", f"attempt {minute}")
                                          for minute in range(25)]}

    result = sagas.detail(PAY, [many], LOOKED_UP, NOW)

    assert (len(result.steps), result.earlier_steps) == (10, 15)
    assert result.steps[0].minutes_ago == 45 and result.steps[-1].minutes_ago == 36


def test_the_same_outcome_many_times_in_a_row_is_one_row_so_the_steps_before_it_stay_visible():
    # 40 retries against a processor that is down, then parked
    retried = PARKED_PAYMENT[0] | {"steps": [
        step("AUTHORIZING", "AUTHORIZED", "2026-10-12T08:07:01"),
        step("AWAITING_CAPTURE", "CAPTURE_REQUESTED", "2026-10-12T08:07:30"),
        *[step("CAPTURING", "UNKNOWN", f"2026-10-12T08:{8 + n // 4:02d}:{15 * (n % 4):02d}", "503 processor_unavailable") for n in range(40)],
        step("CAPTURING", "PARKED", "2026-10-12T08:18:00", "Capture outcome unknown after 10m"),
    ]}

    result = sagas.detail(PAY, [retried], LOOKED_UP, NOW)

    assert [(row.step, row.outcome, row.times) for row in result.steps] == [
        ("AUTHORIZING", "AUTHORIZED", 1), ("AWAITING_CAPTURE", "CAPTURE_REQUESTED", 1),
        ("CAPTURING", "UNKNOWN", 40), ("CAPTURING", "PARKED", 1)]
    assert result.steps[2].minutes_ago == 42  # of the last retry
    assert result.earlier_steps == 0


def test_a_payouts_single_saga_is_read_the_same_way_and_a_missing_status_is_unknown():
    payout_saga = {"sagaId": "4b2d7c1e-0000-4000-8000-000000000002", "payoutId": PAYOUT, "state": "REQUIRES_ATTENTION",
                   "stuckState": "SUBMITTING", "lastError": "unreachable ConnectException", "attempt": 6,
                   "failureCode": "bank_unavailable", "createdAt": "2026-10-12T06:59:00", "finishedAt": None,
                   "steps": [step("HOLDING", "HELD", "2026-10-12T06:59:01"), step("SUBMITTING", "PARKED", "2026-10-12T07:00:00")]}

    result = sagas.detail(PAYOUT, payout_saga, [], NOW)

    assert (result.kind, result.stopped_in, result.status, result.amount) == ("payout", "SUBMITTING", None, None)
    assert result.minutes_since_last_step == 120


def test_the_ids_prefix_says_which_service_to_ask():
    assert sagas.paths(PAY) == (f"/api/v1/payments/{PAY}/saga", "/api/v1/payments/lookup", {"paymentIds": [PAY]})
    assert sagas.paths(PAYOUT) == (f"/api/v1/payouts/{PAYOUT}/saga", "/api/v1/payouts/lookup", {"payoutIds": [PAYOUT]})
    assert sagas.LIST_PARAMS == {"stuckMinutes": 15, "limit": 5}


# ---- over MCP, with PayFlow played by a fake

def over_mcp(tool_name, arguments, monkeypatch, answers):
    asked = []

    async def fake_get(path, params=None):
        asked.append(("GET", path, params))
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def fake_post(path, body):
        asked.append(("POST", path, body))
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(payflow, "get", fake_get)
    monkeypatch.setattr(payflow, "post", fake_post)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools[tool_name], await client.call_tool(tool_name, arguments)

    return (*asyncio.run(call()), asked)


def test_the_list_is_served_over_mcp_and_asks_both_services_for_the_alerts_threshold(monkeypatch):
    tool, result, asked = over_mcp("list_sagas_needing_attention", {}, monkeypatch,
                                   {sagas.PAYMENTS_LIST: PAYMENTS, sagas.PAYOUTS_LIST: PAYOUTS})

    assert tool.input_schema.get("properties", {}) == {}
    assert result.structured_content["payments_parked"] == 1
    assert [saga["id"] for saga in result.structured_content["sagas"]][:2] == [PAYOUT, PAY]
    assert sorted(asked) == [("GET", sagas.PAYMENTS_LIST, {"stuckMinutes": 15, "limit": 5}),
                             ("GET", sagas.PAYOUTS_LIST, {"stuckMinutes": 15, "limit": 5})]


def test_one_service_down_still_lists_the_other_and_both_down_is_an_error(monkeypatch):
    down = httpx.ConnectError("refused")

    _, partial, _ = over_mcp("list_sagas_needing_attention", {}, monkeypatch, {sagas.PAYMENTS_LIST: down, sagas.PAYOUTS_LIST: PAYOUTS})
    _, nothing, _ = over_mcp("list_sagas_needing_attention", {}, monkeypatch, {sagas.PAYMENTS_LIST: down, sagas.PAYOUTS_LIST: down})

    assert not partial.is_error
    assert partial.structured_content["payments_parked"] is None
    assert partial.structured_content["payouts_parked"] == 1
    assert nothing.is_error
    assert "Neither payment-service nor payout-service answered" in nothing.content[0].text


def test_a_missing_secret_is_an_error_the_model_can_read(monkeypatch):
    unavailable = payflow.PayflowUnavailable("PAYFLOW_CLIENT_SECRET is not set for the MCP server")

    _, result, _ = over_mcp("list_sagas_needing_attention", {}, monkeypatch,
                            {sagas.PAYMENTS_LIST: unavailable, sagas.PAYOUTS_LIST: unavailable})

    assert result.is_error
    assert "PAYFLOW_CLIENT_SECRET is not set" in result.content[0].text


def test_get_saga_reads_the_saga_and_the_payments_status(monkeypatch):
    _, result, asked = over_mcp("get_saga", {"id": PAY}, monkeypatch, {
        f"/api/v1/payments/{PAY}/saga": PARKED_PAYMENT, "/api/v1/payments/lookup": LOOKED_UP})

    assert result.structured_content["stopped_in"] == "CAPTURING"
    assert result.structured_content["status"] == "PROCESSING"
    assert asked == [("GET", f"/api/v1/payments/{PAY}/saga", None), ("POST", "/api/v1/payments/lookup", {"paymentIds": [PAY]})]


def test_get_saga_still_answers_when_the_status_lookup_fails(monkeypatch):
    _, result, _ = over_mcp("get_saga", {"id": PAY}, monkeypatch, {
        f"/api/v1/payments/{PAY}/saga": PARKED_PAYMENT, "/api/v1/payments/lookup": httpx.ReadTimeout("slow")})

    assert not result.is_error
    assert result.structured_content["status"] is None
    assert result.structured_content["state"] == "REQUIRES_ATTENTION"


def test_ids_payflow_does_not_have_or_accept_are_explained_to_the_model(monkeypatch):
    _, other_kind, asked = over_mcp("get_saga", {"id": "auth_025cb94d"}, monkeypatch, {})
    _, unknown, _ = over_mcp("get_saga", {"id": PAY}, monkeypatch, {f"/api/v1/payments/{PAY}/saga": payflow.NotFound("x")})
    _, malformed, _ = over_mcp("get_saga", {"id": "pay_123"}, monkeypatch, {"/api/v1/payments/pay_123/saga": payflow.BadRequest("x")})
    _, no_saga, _ = over_mcp("get_saga", {"id": PAY}, monkeypatch, {f"/api/v1/payments/{PAY}/saga": [], "/api/v1/payments/lookup": LOOKED_UP})

    assert other_kind.is_error and "must be a payment id (pay_...) or a payout id (po_...)" in other_kind.content[0].text
    assert asked == []  # PayFlow was not asked
    assert unknown.is_error and f"PayFlow has no payment or payout with the id {PAY}" in unknown.content[0].text
    assert malformed.is_error and "does not accept pay_123 as an id" in malformed.content[0].text
    assert no_saga.is_error and "has no saga" in no_saga.content[0].text
