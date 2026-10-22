"""SagaRequiresAttention: the parked sagas, read with the code each parked for, and sorted by code
into those the agent may propose to retry and those that need a person."""
import asyncio
from datetime import UTC, datetime

import pytest

from agent import config, run, runbooks, sagas
from agent.tools import ToolResult
from agent.trace import load
from mcp_server.clients import payflow
from mcp_server.tools import sagas as saga_tool
from tests.fakes import FakeTools, ScriptedModel, alert
from tests.test_sagas_tool import over_mcp

AT = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
PAY_1 = "pay_11111111-1111-4111-8111-111111111111"
PAY_2 = "pay_22222222-2222-4222-8222-222222222222"
PAY_3 = "pay_33333333-3333-4333-8333-333333333333"


def item(payment_id, saga_id, minutes, reason="PARKED", stuck_state="CAPTURING", error="No capture answer within PT10M"):
    return {"paymentId": payment_id, "sagaId": saga_id, "type": "PAYMENT", "reason": reason,
            "state": "REQUIRES_ATTENTION" if reason == "PARKED" else stuck_state, "stuckState": stuck_state,
            "attempt": 3, "stepStartedAt": "2026-10-22T09:40:00", "minutesInStep": minutes, "failureCode": None,
            "lastError": error, "createdAt": "2026-10-22T09:39:00"}


def step(state, outcome, at):
    return {"state": state, "outcome": outcome, "detail": None, "occurredAt": at}


def saga(saga_id, *steps, state="REQUIRES_ATTENTION", type_="PAYMENT", created="2026-10-22T09:39:00"):
    return {"sagaId": saga_id, "type": type_, "state": state, "stuckState": "CAPTURING", "attempt": 3,
            "failureCode": None, "lastError": "x", "createdAt": created, "finishedAt": None, "steps": list(steps)}


# A payment parked for a capture nobody answered: PayFlow records the code as the step's outcome.
CAPTURE_UNKNOWN = saga("s1", step("AUTHORIZING", "STARTED", "2026-10-22T09:39:00"),
                       step("AUTHORIZING", "AUTHORIZED", "2026-10-22T09:39:01"),
                       step("CAPTURING", "UNKNOWN", "2026-10-22T09:40:00"),
                       step("CAPTURING", "CAPTURE_UNKNOWN", "2026-10-22T09:50:00"))


# ---- the tool: every parked saga of one service, with its code

def test_each_parked_saga_carries_the_outcome_of_its_last_step_and_stuck_ones_are_left_out():
    answer = {"parked": 2, "stuck": 1, "stuckAfterMinutes": 15, "sagas": [
        item(PAY_1, "s1", 20), item(PAY_2, "s2", 45, error="Ledger rejected the settlement: closed account"),
        item(PAY_3, "s3", 30, reason="STUCK", stuck_state=None)]}
    ledger_said_no = saga("s2", step("SETTLING", "SETTLEMENT_REJECTED", "2026-10-22T09:15:00"))
    # a payment's view lists all its sagas: a finished earlier one is not the one asked about
    earlier = saga("s0", step("CAPTURING", "CAPTURED", "2026-10-22T11:00:00"), state="COMPLETED")

    result = saga_tool.parked("payment-service", answer, [[earlier, CAPTURE_UNKNOWN], [ledger_said_no]])

    assert [(s.id, s.parked_for, s.minutes) for s in result.sagas] == [(PAY_2, "SETTLEMENT_REJECTED", 45),
                                                                       (PAY_1, "CAPTURE_UNKNOWN", 20)]
    assert result.parked == 2 and result.not_listed == 0
    assert result.sagas[1].stopped_in == "CAPTURING" and result.sagas[1].kind == "payment"


def test_a_saga_that_could_not_be_read_or_is_no_longer_parked_has_no_code():
    answer = {"parked": 3, "stuck": 0, "stuckAfterMinutes": 15, "sagas": [item(PAY_1, "s1", 20), item(PAY_2, "s2", 10)]}
    retried_meanwhile = saga("s2", step("CAPTURING", "RESUMED", "2026-10-22T09:59:00"), state="CAPTURING")

    result = saga_tool.parked("payment-service", answer, [None, [retried_meanwhile]])

    assert [s.parked_for for s in result.sagas] == [None, None]
    assert result.not_listed == 1       # PayFlow counted three; two were listed


def test_of_two_steps_at_the_same_instant_the_one_recorded_later_counts():
    same_time = saga("s1", step("CAPTURING", "UNKNOWN", "2026-10-22T09:50:00"),
                     step("CAPTURING", "CAPTURE_UNKNOWN", "2026-10-22T09:50:00"))
    answer = {"parked": 1, "stuck": 0, "stuckAfterMinutes": 15, "sagas": [item(PAY_1, "s1", 20)]}

    assert saga_tool.parked("payment-service", answer, [[same_time]]).sagas[0].parked_for == "CAPTURE_UNKNOWN"


def test_over_mcp_it_asks_for_fifty_and_reads_each_parked_saga(monkeypatch):
    answer = {"parked": 2, "stuck": 0, "stuckAfterMinutes": 15, "sagas": [item(PAY_1, "s1", 20), item(PAY_2, "s2", 10)]}
    tool, result, asked = over_mcp("list_parked_sagas", {"service": "payment-service"}, monkeypatch, {
        saga_tool.PAYMENTS_LIST: answer, f"/api/v1/payments/{PAY_1}/saga": [CAPTURE_UNKNOWN],
        f"/api/v1/payments/{PAY_2}/saga": payflow.NotFound("gone")})

    assert not result.is_error
    assert [(s["id"], s["parked_for"]) for s in result.structured_content["sagas"]] == [(PAY_1, "CAPTURE_UNKNOWN"), (PAY_2, None)]
    assert asked[0] == ("GET", saga_tool.PAYMENTS_LIST, {"stuckMinutes": 15, "limit": 50})
    assert tool.input_schema["properties"]["service"]["enum"] == ["payment-service", "payout-service"]


def test_over_mcp_a_payout_is_read_from_payout_service(monkeypatch):
    payout = "po_44444444-4444-4444-8444-444444444444"
    answer = {"parked": 1, "stuck": 0, "stuckAfterMinutes": 15, "sagas": [
        {"payoutId": payout, "sagaId": "p1", "reason": "PARKED", "state": "REQUIRES_ATTENTION", "stuckState": "IN_TRANSIT",
         "attempt": 1, "stepStartedAt": "2026-10-22T08:00:00", "minutesInStep": 120, "failureCode": None,
         "lastError": "Transfer t1 still in transit after PT1H", "createdAt": "2026-10-22T07:59:00"}]}
    view = {"sagaId": "p1", "payoutId": payout, "state": "REQUIRES_ATTENTION", "stuckState": "IN_TRANSIT",
            "steps": [step("IN_TRANSIT", "IN_TRANSIT_TOO_LONG", "2026-10-22T09:00:00")]}

    _, result, _ = over_mcp("list_parked_sagas", {"service": "payout-service"}, monkeypatch,
                            {saga_tool.PAYOUTS_LIST: answer, f"/api/v1/payouts/{payout}/saga": view})

    assert result.structured_content["sagas"][0] | {} == {"id": payout, "kind": "payout", "stopped_in": "IN_TRANSIT",
                                                           "parked_for": "IN_TRANSIT_TOO_LONG", "minutes": 120,
                                                           "last_error": "Transfer t1 still in transit after PT1H"}


# ---- the runbook: which code means which cause, and which causes may be retried

def test_the_runbook_maps_every_parking_code_payflow_has_and_retries_only_the_unanswered():
    loaded = runbooks.load("SagaRequiresAttention", config.RUNBOOKS_DIR)

    assert loaded.retryable == ("processor_no_answer", "bank_no_answer")
    assert loaded.parking_codes["processor_no_answer"] == ("CAPTURE_UNKNOWN", "VOID_UNKNOWN", "REFUND_UNKNOWN")
    assert loaded.parking_codes["bank_no_answer"] == ("TRANSFER_UNKNOWN", "TRANSFER_STATUS_UNKNOWN", "IN_TRANSIT_TOO_LONG")
    every_code = [code for codes in loaded.parking_codes.values() for code in codes]
    assert len(every_code) == 16 == len(set(every_code))
    # the other runbook has none: its workflow does not sort sagas
    assert runbooks.load("ProcessorCircuitOpen", config.RUNBOOKS_DIR).parking_codes == {}


@pytest.mark.parametrize(("wrong", "right", "why"), [
    ("  ledger_rejected: [SETTLEMENT_REJECTED,", "  ledger_rejected: [CAPTURE_UNKNOWN, SETTLEMENT_REJECTED,",
     "CAPTURE_UNKNOWN is listed for both processor_no_answer and ledger_rejected"),
    ("  ledger_rejected: [SETTLEMENT_REJECTED,", "  cosmic_rays: [SETTLEMENT_REJECTED,", "parking_codes names a cause"),
    ("  ledger_rejected: [SETTLEMENT_REJECTED,", "  ledger_rejected: [settlement_rejected,", "must be written as PayFlow records it"),
    ("retryable: [processor_no_answer, bank_no_answer]", "retryable: [processor_no_answer, unknown]",
     "retryable names a cause without parking codes"),
])
def test_parking_codes_and_retryable_causes_are_checked_when_the_runbook_is_read(wrong, right, why):
    text = (config.RUNBOOKS_DIR / "SagaRequiresAttention.md").read_text(encoding="utf-8")
    changed = text.replace(wrong, right, 1)
    assert changed != text

    with pytest.raises(runbooks.InvalidRunbook, match=why):
        runbooks.parse(changed, "SagaRequiresAttention.md")


# ---- the sorting, by code

RUNBOOK = runbooks.load("SagaRequiresAttention", config.RUNBOOKS_DIR).as_dict()


def parked(service, *rows, not_listed=0):
    return {"service": service, "parked": len(rows) + not_listed, "not_listed": not_listed,
            "sagas": [{"id": id_, "kind": "payment", "stopped_in": "CAPTURING", "parked_for": code, "minutes": minutes,
                       "last_error": "x"} for id_, code, minutes in rows]}


def test_only_sagas_parked_without_an_answer_go_on_the_retry_list():
    sorted_ = sagas.sort({"payment-service": parked("payment-service", (PAY_1, "CAPTURE_UNKNOWN", 20),
                                                    (PAY_2, "SETTLEMENT_REJECTED", 45), (PAY_3, None, 30),
                                                    ("pay_4", "SOMETHING_NEW", 5), ("pay_5", "REFUND_UNKNOWN", 40)),
                          "payout-service": parked("payout-service", ("po_1", "IN_TRANSIT_TOO_LONG", 90))}, RUNBOOK)

    assert [(row["id"], row["cause"]) for row in sorted_["retry"]] == [
        ("po_1", "bank_no_answer"), ("pay_5", "processor_no_answer"), (PAY_1, "processor_no_answer")]
    assert [(row["id"], row["cause"]) for row in sorted_["needs_a_person"]] == [
        (PAY_2, "ledger_rejected"), (PAY_3, None), ("pay_4", None)]
    assert sorted_["over_the_limit"] == 0 and sorted_["unread"] == {}


def test_at_most_fifty_are_proposed_the_oldest_first_and_the_rest_are_counted():
    rows = [(f"pay_{n}", "CAPTURE_UNKNOWN", n) for n in range(60)]
    sorted_ = sagas.sort({"payment-service": parked("payment-service", *rows, not_listed=7)}, RUNBOOK)

    assert len(sorted_["retry"]) == 50 and sorted_["retry"][0]["id"] == "pay_59"
    assert sorted_["over_the_limit"] == 10 and sorted_["not_listed"] == 7


def test_a_service_whose_sagas_could_not_be_read_is_reported_not_guessed():
    sorted_ = sagas.sort({"payment-service": "PayFlow did not answer"}, RUNBOOK)

    assert sorted_ == {"retry": [], "needs_a_person": [], "not_listed": 0, "over_the_limit": 0,
                       "unread": {"payment-service": "PayFlow did not answer"}}


def test_the_services_read_are_those_the_alert_is_about():
    assert sagas.services_of([alert("SagaRequiresAttention", application="payout-service")]) == ["payout-service"]
    assert sagas.services_of([alert("SagaRequiresAttention", application="payment-service"),
                              alert("SagaRequiresAttention", application="payout-service")]) == list(sagas.SERVICES)
    assert sagas.services_of([alert("SagaRequiresAttention")]) == list(sagas.SERVICES)


# ---- the workflow and the report

PARKED_ALERT = alert("SagaRequiresAttention", application="payment-service")
LIST = {"payments_parked": 2, "concerns": ["2 payment saga(s) are parked and wait for an operator's retry"],
        "sagas": [{"id": PAY_1, "last_error": "No capture answer within PT10M"}]}
NO_ANSWER = {"root_cause": "processor_no_answer", "confidence": "high", "summary": "The processor did not answer captures.",
             "evidence": [{"check": "parked_sagas", "quote": "No capture answer within PT10M"}]}


def investigate(tmp_path, diagnosis, read):
    tools = FakeTools([PARKED_ALERT], list_sagas_needing_attention=LIST, list_parked_sagas=read)
    model = ScriptedModel(["parked_sagas"], diagnoses=[diagnosis])
    trace = asyncio.run(run.investigate("SagaRequiresAttention", tools, model, tmp_path, now=lambda: AT))
    return trace, tools


def test_the_proposal_carries_the_retry_list_code_made_and_the_report_shows_both_lists(tmp_path):
    read = parked("payment-service", (PAY_1, "CAPTURE_UNKNOWN", 20), (PAY_2, "SETTLEMENT_REJECTED", 45))

    trace, tools = investigate(tmp_path, NO_ANSWER, read)

    document = load(trace.path)
    assert ("list_parked_sagas", {"service": "payment-service"}) in tools.asked
    proposal = document["result"]["proposal"]
    assert proposal["cause"] == "processor_no_answer" and "once the processor answers again" in proposal["step"]
    assert [row["id"] for row in proposal["retry"]] == [PAY_1]
    report = trace.report_path.read_text(encoding="utf-8")
    assert "**To retry (1):**" in report and f"`{PAY_1}`" in report and "`CAPTURE_UNKNOWN`" in report
    assert "**Needs a person (1):**" in report and "`SETTLEMENT_REJECTED`" in report


def test_whatever_the_model_names_the_list_is_codes(tmp_path):
    """The model blames the ledger; code still finds one saga to retry, and proposes exactly it."""
    ledger = {"root_cause": "ledger_rejected", "confidence": "high", "summary": "The ledger refused a settlement.",
              "evidence": [{"check": "parked_sagas", "quote": "No capture answer within PT10M"}]}
    read = parked("payment-service", (PAY_1, "CAPTURE_UNKNOWN", 20), (PAY_2, "SETTLEMENT_REJECTED", 45))

    trace, _ = investigate(tmp_path, ledger, read)

    proposal = load(trace.path)["result"]["proposal"]
    assert proposal["step"] == sagas.RETRY_STEP and [row["id"] for row in proposal["retry"]] == [PAY_1]


def test_a_retryable_cause_with_nothing_to_retry_proposes_nothing(tmp_path):
    trace, _ = investigate(tmp_path, NO_ANSWER, parked("payment-service", (PAY_2, "SETTLEMENT_REJECTED", 45)))

    document = load(trace.path)
    assert document["result"]["proposal"] is None
    assert "**Needs a person (1):**" in trace.report_path.read_text(encoding="utf-8")


def test_a_refusal_cause_proposes_the_runbooks_step_for_a_person_without_a_retry_list(tmp_path):
    refused = {"root_cause": "ledger_rejected", "confidence": "high", "summary": "The ledger refused a settlement.",
               "evidence": [{"check": "parked_sagas", "quote": "No capture answer within PT10M"}]}

    trace, _ = investigate(tmp_path, refused, parked("payment-service", (PAY_2, "SETTLEMENT_REJECTED", 45)))

    proposal = load(trace.path)["result"]["proposal"]
    assert "retry" not in proposal and "Compare the ledger's postings" in proposal["step"]


def test_sagas_that_could_not_be_read_are_said_so_and_nothing_is_proposed(tmp_path):
    trace, _ = investigate(tmp_path, NO_ANSWER, ToolResult(True, "PayFlow did not answer at http://localhost"))

    assert load(trace.path)["result"]["proposal"] is None
    assert "The parked sagas of payment-service could not be read: PayFlow did not answer" in \
        trace.report_path.read_text(encoding="utf-8")


def test_the_processor_runbook_does_not_sort_sagas(tmp_path):
    tools = FakeTools([alert("ProcessorCircuitOpen", application="payment-service")])
    trace = asyncio.run(run.investigate("ProcessorCircuitOpen", tools, ScriptedModel(), tmp_path, now=lambda: AT))

    assert "sort_sagas" not in [step["step"] for step in load(trace.path)["steps"]]
    assert all(tool != "list_parked_sagas" for tool, _ in tools.asked)
