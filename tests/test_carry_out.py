"""Carrying out an approved retry list: each saga checked again and retried one at a time, then
watched until it finishes or parks again; a restart goes on where it stopped."""
import asyncio
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent import carry_out, run
from agent.operator import OperatorUnavailable, Refused
from agent.tools import ToolResult
from agent.trace import load
from api import store as states
from api.service import Service
from api.store import Store
from tests.fakes import FakeTools, ScriptedModel, alert

AT = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
PAY_1 = "pay_11111111-1111-4111-8111-111111111111"
PAY_2 = "pay_22222222-2222-4222-8222-222222222222"
APPROVED = {"decision": "approved", "by": "alice", "at": "2026-10-22T10:05:00Z", "note": "processor is back"}


def partners(processor_calls=10, processor_unanswered=0, bank_calls=0, bank_unanswered=0, breaker="closed"):
    def partner(calls, unanswered):
        return {"called_by": "x", "calls": calls, "succeeded": calls - unanswered, "declined": 0, "rejected": 0,
                "unanswered": unanswered, "unanswered_percent": 0.0, "avg_ms": 20, "operations": []}
    return {"minutes": 2, "processor": partner(processor_calls, processor_unanswered), "bank": partner(bank_calls, bank_unanswered),
            "processor_circuit_breaker": {"state": breaker, "instances": 2, "instances_not_closed": 0 if breaker == "closed" else 2,
                                          "opened_in_window": False, "calls_refused": 0, "recent_failure_percent": None},
            "concerns": []}


PODS_READY = {"workload": "processor-simulator", "kind": "Deployment", "pods_wanted": 1, "pods_ready": 1, "healthy_pods": 1,
              "unhealthy_pods": [], "recent_warnings": [], "concerns": []}
HEALTHY = {"service": "processor-simulator", "instances_up": 1, "server_error_percent": 0.0, "concerns": []}


class ChangingTools:
    """Tools whose answers can change between calls (a partner going down midway)."""

    def __init__(self, **answers):
        self.answers = answers
        self.asked = []

    async def call(self, tool, arguments=None):
        self.asked.append((tool, arguments or {}))
        answer = self.answers[tool]
        return answer if isinstance(answer, ToolResult) else ToolResult(False, answer)


class FakeOperator:
    """PayFlow as the operator login sees it. A retried saga runs for `runs_for` reads, then ends as `ends_as`."""

    def __init__(self, sagas, runs_for=1, ends_as="COMPLETED", fail_retry_of=()):
        self.sagas = {id_: dict(saga) for id_, saga in sagas.items()}
        self.runs_for = runs_for
        self.ends_as = ends_as
        self.fail_retry_of = set(fail_retry_of)
        self.retried = []
        self.reads = {}

    async def saga(self, id_, saga=None):
        if id_ not in self.sagas:
            raise Refused(f"PayFlow refused GET {id_}: Payment not found")
        now = self.sagas[id_]
        if now["state"] == "RUNNING":
            self.reads[id_] = self.reads.get(id_, 0) + 1
            if self.reads[id_] > self.runs_for:
                now |= {"state": self.ends_as, "finished": self.ends_as != "REQUIRES_ATTENTION",
                        "parked_for": "CAPTURE_UNKNOWN" if self.ends_as == "REQUIRES_ATTENTION" else None}
        return {"id": id_, "saga": "s-" + id_, "stuck_state": None, "failure_code": None, "last_error": None} | now

    async def retry(self, id_):
        if id_ in self.fail_retry_of:
            self.fail_retry_of.discard(id_)
            raise RuntimeError("the service stopped here")
        if self.sagas[id_]["state"] != "REQUIRES_ATTENTION":
            raise Refused(f"PayFlow refused POST {id_}: The payment has no saga waiting for an operator")
        self.retried.append(id_)
        self.sagas[id_] |= {"state": "RUNNING", "parked_for": None}
        return await self.saga(id_)


def parked(code="CAPTURE_UNKNOWN"):
    return {"state": "REQUIRES_ATTENTION", "parked_for": code, "finished": False}


def row(id_, code="CAPTURE_UNKNOWN", cause="processor_no_answer"):
    return {"id": id_, "kind": "payment", "stopped_in": "CAPTURING", "parked_for": code, "cause": cause, "minutes": 20}


# ---- does the partner answer now?

def test_the_processor_answers_when_its_recent_calls_were_all_answered_and_its_breaker_is_closed():
    answers, why = asyncio.run(carry_out.answering("processor", ChangingTools(get_dependency_status=partners())))
    assert answers and why == "all 10 calls to the processor in the last 2 minutes were answered"


@pytest.mark.parametrize(("status", "why"), [
    (partners(breaker="open"), "the processor circuit breaker is open on 2 of 2"),
    (partners(breaker="half_open"), "is half_open"),
    (partners(processor_unanswered=1), "1 of 10 calls to the processor got no answer"),
    (ToolResult(True, "Prometheus did not answer"), "its calls could not be read: Prometheus did not answer"),
])
def test_the_processor_does_not_answer_yet(status, why):
    answers, said = asyncio.run(carry_out.answering("processor", ChangingTools(get_dependency_status=status)))
    assert not answers and why in said


def test_with_nobody_calling_the_partner_its_own_health_decides():
    quiet = partners(processor_calls=0)
    tools = ChangingTools(get_dependency_status=quiet, get_pod_status=PODS_READY, get_service_health=HEALTHY)
    answers, why = asyncio.run(carry_out.answering("processor", tools))
    assert answers and "nobody called the processor" in why and "pods are all ready" in why
    assert ("get_pod_status", {"workload": "processor-simulator"}) in tools.asked

    for pods, health, said in ((PODS_READY | {"pods_ready": 0}, HEALTHY, "has 0 of 1 pods ready"),
                               (PODS_READY | {"pods_wanted": 0, "pods_ready": 0}, HEALTHY, "has 0 of 0 pods ready"),
                               (PODS_READY, HEALTHY | {"server_error_percent": 50.0}, "50% of requests with a 5xx"),
                               (PODS_READY, ToolResult(True, "no answer"), "its health could not be read")):
        tools = ChangingTools(get_dependency_status=quiet, get_pod_status=pods, get_service_health=health)
        answers, why = asyncio.run(carry_out.answering("processor", tools))
        assert not answers and said in why


def test_the_bank_is_judged_by_its_own_calls_not_the_processors_breaker():
    status = partners(breaker="open", bank_calls=4)
    assert asyncio.run(carry_out.answering("bank", ChangingTools(get_dependency_status=status)))[0]
    status = partners(bank_calls=4, bank_unanswered=4)
    assert not asyncio.run(carry_out.answering("bank", ChangingTools(get_dependency_status=status)))[0]


# ---- one saga, checked again and retried

def test_a_saga_still_parked_for_the_same_code_with_its_partner_answering_is_retried():
    operator = FakeOperator({PAY_1: parked()})
    done = asyncio.run(carry_out.retry_one(row(PAY_1), operator, ChangingTools(get_dependency_status=partners())))
    assert done["outcome"] == "retried" and operator.retried == [PAY_1] and done["saga"] == "s-" + PAY_1


@pytest.mark.parametrize(("saga", "tools", "why"), [
    ({"state": "CAPTURING", "parked_for": None, "finished": False}, partners(), "no longer parked: it is CAPTURING"),
    ({"state": "COMPLETED", "parked_for": None, "finished": True}, partners(), "no longer parked: it is COMPLETED (finished)"),
    (parked("CAPTURE_REJECTED"), partners(), "parked for CAPTURE_REJECTED now, not CAPTURE_UNKNOWN as proposed"),
    (parked(), partners(breaker="open"), "the processor does not answer yet: the processor circuit breaker is open"),
])
def test_a_saga_that_changed_or_whose_partner_is_still_down_is_skipped_and_never_retried(saga, tools, why):
    operator = FakeOperator({PAY_1: saga})
    done = asyncio.run(carry_out.retry_one(row(PAY_1), operator, ChangingTools(get_dependency_status=tools)))
    assert (done["outcome"], operator.retried) == ("skipped", []) and why in done["why"]


def test_payflow_refusing_or_not_answering_is_recorded_for_that_saga():
    class Refusing(FakeOperator):
        async def retry(self, id_):
            raise Refused("PayFlow refused POST: The payment has no saga waiting for an operator")

    class Away(FakeOperator):
        async def saga(self, id_, saga=None):
            raise OperatorUnavailable("PayFlow did not answer GET (ConnectError)")

    tools = ChangingTools(get_dependency_status=partners())
    refused = asyncio.run(carry_out.retry_one(row(PAY_1), Refusing({PAY_1: parked()}), tools))
    away = asyncio.run(carry_out.retry_one(row(PAY_1), Away({PAY_1: parked()}), tools))
    assert (refused["outcome"], away["outcome"]) == ("refused", "not_reached")
    assert "no saga waiting" in refused["why"] and "did not answer" in away["why"]


# ---- the workflow: approve, retry each, watch

LIST = {"payments_parked": 2, "concerns": ["2 payment saga(s) are parked and wait for an operator's retry"],
        "sagas": [{"id": PAY_1, "last_error": "No capture answer within PT10M"}]}
NO_ANSWER = {"root_cause": "processor_no_answer", "confidence": "high", "summary": "The processor did not answer captures.",
             "evidence": [{"check": "parked_sagas", "quote": "No capture answer within PT10M"}]}
TWO_PARKED = {"service": "payment-service", "parked": 2, "not_listed": 0, "sagas": [
    {"id": PAY_1, "kind": "payment", "stopped_in": "CAPTURING", "parked_for": "CAPTURE_UNKNOWN", "minutes": 30, "last_error": "x"},
    {"id": PAY_2, "kind": "payment", "stopped_in": "CAPTURING", "parked_for": "CAPTURE_UNKNOWN", "minutes": 20, "last_error": "x"}]}


class Clock:
    """A clock the watch's sleeps move forward, so five minutes pass at once."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def paused(tmp_path, saver):
    tools = FakeTools([alert("SagaRequiresAttention", application="payment-service")], list_sagas_needing_attention=LIST,
                      list_parked_sagas=TWO_PARKED)
    model = ScriptedModel(["parked_sagas"], diagnoses=[NO_ANSWER])
    trace = asyncio.run(run.investigate("SagaRequiresAttention", tools, model, tmp_path, now=lambda: AT, checkpointer=saver,
                                        ask_for_decision=True))
    assert load(trace.path)["status"] == "awaiting_decision"
    return trace


def resume(trace, saver, operator, tools=None, decision=APPROVED, clock=None):
    clock = clock or Clock()
    tools = tools or ChangingTools(get_dependency_status=partners())
    return asyncio.run(run.resume(trace.path, decision, saver, now=lambda: AT, tools=tools, operator=operator,
                                  sleep=clock.sleep, clock=clock))


def test_an_approval_retries_each_saga_one_step_at_a_time_and_watches_them_finish(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)
    operator = FakeOperator({PAY_1: parked(), PAY_2: parked()}, runs_for=2)

    done = resume(trace, saver, operator)

    document = load(done.path)
    assert document["status"] == "completed" and operator.retried == [PAY_1, PAY_2]
    assert [step["step"] for step in document["steps"]][-4:] == ["decide", "retry", "retry", "watch"]
    carried = document["result"]["carried_out"]
    assert [(item["id"], item["outcome"], item["ended"]) for item in carried] == [
        (PAY_1, "retried", "finished: COMPLETED"), (PAY_2, "retried", "finished: COMPLETED")]
    report = done.report_path.read_text(encoding="utf-8")
    assert "### What was carried out" in report and "2 of 2 were retried" in report and "finished: COMPLETED" in report
    assert "it takes no other action" in report


def test_a_saga_still_running_after_five_minutes_or_parked_again_is_reported_as_such(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)
    clock = Clock()

    done = resume(trace, saver, FakeOperator({PAY_1: parked(), PAY_2: parked()}, runs_for=10_000), clock=clock)

    carried = load(done.path)["result"]["carried_out"]
    assert [item["ended"] for item in carried] == ["still running, in RUNNING after 5 minutes of watching"] * 2
    assert clock.now >= carry_out.WATCH_SECONDS

    saver = InMemorySaver()
    trace = paused(tmp_path / "again", saver)
    done = resume(trace, saver, FakeOperator({PAY_1: parked(), PAY_2: parked()}, ends_as="REQUIRES_ATTENTION"))
    assert {item["ended"] for item in load(done.path)["result"]["carried_out"]} == {"parked again, for CAPTURE_UNKNOWN"}


def test_when_the_partner_stops_answering_midway_the_rest_are_skipped(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)
    tools = ChangingTools(get_dependency_status=partners())
    operator = FakeOperator({PAY_1: parked(), PAY_2: parked()})
    first_retry = operator.retry

    async def retry_then_outage(id_):
        tools.answers["get_dependency_status"] = partners(processor_unanswered=3)
        return await first_retry(id_)
    operator.retry = retry_then_outage

    done = resume(trace, saver, operator, tools=tools)

    carried = load(done.path)["result"]["carried_out"]
    assert [item["outcome"] for item in carried] == ["retried", "skipped"] and operator.retried == [PAY_1]
    assert "3 of 10 calls to the processor got no answer" in carried[1]["why"]


def test_a_restart_in_the_middle_goes_on_with_the_next_saga_and_retries_none_twice(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)
    operator = FakeOperator({PAY_1: parked(), PAY_2: parked()}, fail_retry_of=[PAY_2])

    with pytest.raises(RuntimeError, match="the service stopped here"):
        resume(trace, saver, operator)
    assert operator.retried == [PAY_1] and load(trace.path)["status"] == "awaiting_decision"

    done = resume(trace, saver, operator)                       # the service, started again

    assert operator.retried == [PAY_1, PAY_2]
    document = load(done.path)
    assert [item["outcome"] for item in document["result"]["carried_out"]] == ["retried", "retried"]
    assert [step["step"] for step in document["steps"]].count("decide") == 1     # the decision is not taken again


def test_a_rejection_retries_nothing(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)
    operator = FakeOperator({PAY_1: parked(), PAY_2: parked()})

    done = resume(trace, saver, operator, decision={"decision": "rejected", "by": "bob", "at": "2026-10-22T10:05:00Z",
                                                    "note": "looking myself"})

    assert operator.retried == [] and "carried_out" not in load(done.path)["result"]


def test_an_approved_retry_list_without_the_operator_login_fails_rather_than_pretending(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)

    with pytest.raises(Exception, match="needs the operator login"):
        resume(trace, saver, None)


# ---- the service: approving answers at once; the second worker carries it out

def test_the_service_answers_an_approval_at_once_and_carries_it_out_in_the_background(tmp_path):
    saver = InMemorySaver()
    trace = paused(tmp_path, saver)
    operator = FakeOperator({PAY_1: parked(), PAY_2: parked()})
    resumed = []

    async def resume_one(trace_id, decision):
        resumed.append(decision)
        clock = Clock()
        return await run.resume(tmp_path / f"{trace_id}.json", decision, saver, tools=ChangingTools(
            get_dependency_status=partners()), operator=operator, sleep=clock.sleep, clock=clock)

    store = Store(tmp_path / "agent.sqlite")
    service = Service(store, None, lambda name: True, resume=resume_one)
    investigation_id = store.create("SagaRequiresAttention", states.AWAITING_DECISION)
    store.update(investigation_id, trace_id=trace.id, to_retry=2)

    decided = asyncio.run(service.decide(investigation_id, states.APPROVED, by="alice"))

    assert decided["status"] == "carrying_out" and resumed == [] and operator.retried == []
    assert asyncio.run(service.carry_out_one()) == investigation_id
    assert store.investigation(investigation_id)["status"] == "completed" and operator.retried == [PAY_1, PAY_2]
    assert resumed[0]["by"] == "alice"


def test_with_the_mcp_server_away_an_approval_waits_and_is_tried_again(tmp_path):
    class ConnectError(Exception):
        pass

    async def unreachable(trace_id, decision):
        raise ConnectError("refused")

    store = Store(tmp_path / "agent.sqlite")
    service = Service(store, None, lambda name: True, resume=unreachable)
    investigation_id = store.create("SagaRequiresAttention", states.CARRYING_OUT)
    store.update(investigation_id, trace_id="t", decision="approved", decided_by="alice", decided_at="2026-10-22T10:05:00Z")

    with pytest.raises(Exception):     # noqa: B017 - the service's own "postponed" signal
        asyncio.run(service.carry_out_one())

    found = store.investigation(investigation_id)
    assert found["status"] == "carrying_out" and found["error"].startswith("waiting: the MCP server")
