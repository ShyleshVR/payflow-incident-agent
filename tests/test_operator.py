import asyncio

import httpx
import pytest

from agent.operator import Operator, OperatorUnavailable, Refused


class FakePayflow:
    """PayFlow's token endpoint and saga endpoints, as the operator login sees them."""

    def __init__(self, sagas, expires_in=300):
        self.sagas = sagas                      # id -> the view PayFlow answers (a list for a payment)
        self.expires_in = expires_in
        self.tokens_issued = 0
        self.revoked = set()
        self.requests = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            if request.headers.get("Authorization") != httpx.BasicAuth("incident-agent-operator", "s3cret")._auth_header:
                return httpx.Response(401)
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens_issued}", "expires_in": self.expires_in})
        if request.headers.get("Authorization", "").removeprefix("Bearer ") in self.revoked:
            return httpx.Response(401)
        self.requests.append((request.method, request.url.path))
        saga_id = request.url.path.split("/")[4]
        if saga_id not in self.sagas:
            return httpx.Response(404, json={"message": f"no payment {saga_id}"})
        view = self.sagas[saga_id]
        if request.method == "POST":
            parked = [s for s in (view if isinstance(view, list) else [view]) if s["state"] == "REQUIRES_ATTENTION"]
            if not parked:
                return httpx.Response(409, json={"message": "Saga is not waiting for an operator"})
            parked[0]["state"] = "CAPTURING"
        return httpx.Response(200, json=view)


def operator(payflow, secret="s3cret"):
    http = httpx.AsyncClient(base_url="http://payflow", transport=httpx.MockTransport(payflow.handle))
    return Operator("http://payflow", "incident-agent-operator", secret, http=http)


def payment_saga(state, created="2026-10-16T10:00:00", **fields):
    return {"sagaId": "s", "type": "PAYMENT", "state": state, "stuckState": fields.get("stuck"), "failureCode": None,
            "lastError": fields.get("error"), "createdAt": created, "finishedAt": fields.get("finished")}


def test_a_payment_reads_as_its_parked_saga_even_when_a_refund_is_newer():
    payflow = FakePayflow({"pay_1": [payment_saga("REQUIRES_ATTENTION", stuck="CAPTURING", error="processor unreachable"),
                                     payment_saga("COMPLETED", created="2026-10-16T11:00:00", finished="2026-10-16T11:00:05")]})

    saga = asyncio.run(operator(payflow).saga("pay_1"))

    assert saga == {"id": "pay_1", "state": "REQUIRES_ATTENTION", "stuck_state": "CAPTURING", "failure_code": None,
                    "last_error": "processor unreachable", "finished": False}
    assert payflow.requests == [("GET", "/api/v1/payments/pay_1/saga")]


def test_without_a_parked_saga_a_payment_reads_as_its_newest():
    payflow = FakePayflow({"pay_1": [payment_saga("COMPLETED", finished="x"),
                                     payment_saga("REFUNDING", created="2026-10-16T12:00:00")]})

    assert asyncio.run(operator(payflow).saga("pay_1"))["state"] == "REFUNDING"


def test_a_parked_payout_is_retried_through_its_own_endpoint():
    payflow = FakePayflow({"po_7": {"sagaId": "s", "payoutId": "po_7", "state": "REQUIRES_ATTENTION", "stuckState": "SENDING",
                                    "lastError": "bank unreachable", "failureCode": None, "createdAt": "t", "finishedAt": None}})

    saga = asyncio.run(operator(payflow).retry("po_7"))

    assert saga["state"] == "CAPTURING" and saga["id"] == "po_7"
    assert payflow.requests == [("POST", "/api/v1/payouts/po_7/saga/retry")]


def test_payflow_refusing_a_saga_that_is_not_parked_is_a_refusal_not_an_outage():
    payflow = FakePayflow({"pay_1": [payment_saga("COMPLETED", finished="x")]})

    with pytest.raises(Refused, match="not waiting for an operator"):
        asyncio.run(operator(payflow).retry("pay_1"))


def test_an_unknown_payment_is_a_refusal():
    with pytest.raises(Refused, match="no payment pay_9"):
        asyncio.run(operator(FakePayflow({})).saga("pay_9"))


def test_an_id_that_is_neither_a_payment_nor_a_payout_reaches_nothing():
    payflow = FakePayflow({})

    with pytest.raises(ValueError):
        asyncio.run(operator(payflow).retry("../../ledger"))
    assert payflow.requests == [] and payflow.tokens_issued == 0


def test_one_token_serves_many_calls_and_a_revoked_one_is_replaced_once():
    payflow = FakePayflow({"pay_1": [payment_saga("REQUIRES_ATTENTION")]})
    login = operator(payflow)

    async def calls():
        await login.saga("pay_1")
        await login.saga("pay_1")
        payflow.revoked.add("token-1")
        await login.saga("pay_1")

    asyncio.run(calls())

    assert payflow.tokens_issued == 2


def test_a_token_close_to_expiring_is_renewed_before_use():
    payflow = FakePayflow({"pay_1": [payment_saga("REQUIRES_ATTENTION")]}, expires_in=30)      # within the 60 s margin
    login = operator(payflow)

    async def calls():
        await login.saga("pay_1")
        await login.saga("pay_1")

    asyncio.run(calls())

    assert payflow.tokens_issued == 2


def test_a_wrong_secret_or_none_is_reported_without_the_secret():
    with pytest.raises(OperatorUnavailable, match="refused the client id or secret of incident-agent-operator") as wrong:
        asyncio.run(operator(FakePayflow({}), secret="wrong-s3cret").saga("pay_1"))
    assert "wrong-s3cret" not in str(wrong.value)

    with pytest.raises(OperatorUnavailable, match="write_env.py"):
        asyncio.run(operator(FakePayflow({}), secret="").saga("pay_1"))


def test_payflow_not_answering_is_unavailable():
    def down(request):
        raise httpx.ConnectError("refused")
    login = Operator("http://payflow", "incident-agent-operator", "s3cret",
                     http=httpx.AsyncClient(base_url="http://payflow", transport=httpx.MockTransport(down)))

    with pytest.raises(OperatorUnavailable, match="did not answer"):
        asyncio.run(login.retry("pay_1"))
