"""ReconciliationRunFailed: the failed days, sorted by code by the source each one's error names,
and an approved rerun list carried out: each day checked again, run again, and watched."""
import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from mcp import Client

from agent import carry_out, config, diagnosis, reruns, run, runbooks
from agent.operator import Operator, OperatorUnavailable, Refused
from agent.trace import load
from mcp_server import server as mcp_server_module
from mcp_server.clients import payflow
from mcp_server.tools import alerts, reconciliation
from tests.fakes import FakeTools, ScriptedModel, alert
from tests.test_carry_out import HEALTHY, PODS_READY, ChangingTools, Clock
from tests.test_reconciliation_tool import NOW, TODAY, WEEK, week
from tests.test_reconciliation_tool import run as run_of

RUNBOOK = runbooks.load("ReconciliationRunFailed", config.RUNBOOKS_DIR).as_dict()
LEDGER_DOWN = ('org.springframework.web.client.ResourceAccessException: I/O error on GET request for '
               '"http://ledger-service:8082/api/v1/ledger/transactions": Connection refused')
PROCESSOR_DOWN = ('org.springframework.web.client.ResourceAccessException: I/O error on GET request for '
                  '"http://processor-simulator:8086/v1/reports/settlement/authorizations": null')
ABANDONED = "Abandoned: the run stopped reporting (its pod was probably killed); taken over by a new run"


# ---- the tool: every day no run has reconciled, whose newest run failed

def test_a_day_is_listed_only_while_no_run_has_reconciled_it_and_its_newest_run_failed():
    d11, d10, d9 = WEEK[0], WEEK[1], WEEK[2]
    runs = week(d11=[run_of(d11, "FAILED", error=LEDGER_DOWN), run_of(d11, "FAILED", started="03:00:00", error=PROCESSOR_DOWN)],
                d10=[run_of(d10), run_of(d10, "FAILED", started="05:00:00", trigger="MANUAL", error=LEDGER_DOWN)],
                d9=[run_of(d9, "FAILED", error=LEDGER_DOWN), run_of(d9, "RUNNING", started="03:00:00")])

    failed = reconciliation.failed(runs, NOW)

    assert [(day.date, day.failed_runs, day.last_error) for day in failed.days] == [(d11.isoformat(), 2, PROCESSOR_DOWN)]
    assert failed.days_read == 8


def test_today_counts_too_and_a_long_error_keeps_its_host():
    long = "x" * 120 + ' I/O error on POST request for "http://merchant-service:8084/oauth2/token": Connection refused'
    failed = reconciliation.failed(week() | {TODAY: [run_of(TODAY, "FAILED", on=TODAY, error=long)]}, NOW)
    assert failed.days[0].date == TODAY.isoformat() and "merchant-service" in failed.days[0].last_error


def test_over_mcp_it_reads_today_and_the_last_week(monkeypatch):
    d11 = WEEK[0]
    runs = week(d11=[run_of(d11, "FAILED", error=LEDGER_DOWN)])
    asked = []

    async def by_day(path, params=None):
        asked.append(params["date"])
        return runs[datetime.fromisoformat(params["date"]).date()]
    monkeypatch.setattr(payflow, "get", by_day)
    monkeypatch.setattr(alerts, "utc_now", lambda: NOW)

    async def call():
        async with Client(mcp_server_module.server) as client:
            return await client.call_tool("list_failed_reconciliations", {})

    result = asyncio.run(call())

    assert not result.is_error and [day["date"] for day in result.structured_content["days"]] == [d11.isoformat()]
    assert len(asked) == 8 and TODAY.isoformat() in asked


# ---- the runbook and the sorting

def test_the_runbook_names_each_source_once_and_reruns_every_known_one():
    loaded = runbooks.load("ReconciliationRunFailed", config.RUNBOOKS_DIR)
    assert set(loaded.rerunnable) == set(loaded.error_sources) and len(loaded.error_sources) == 6
    assert loaded.error_sources["run_abandoned"] == ("Abandoned",)


@pytest.mark.parametrize(("wrong", "right", "why"), [
    ("  run_abandoned: [\"Abandoned\"]", "  run_abandoned: [ledger-service]", "listed for both ledger_unavailable and run_abandoned"),
    ("  run_abandoned: [\"Abandoned\"]", "  cosmic_rays: [\"Abandoned\"]", "error_sources names a cause"),
    ("rerunnable: [processor_unavailable,", "rerunnable: [unknown, processor_unavailable,", "rerunnable names a cause without"),
])
def test_error_sources_are_checked_when_the_runbook_is_read(wrong, right, why):
    text = (config.RUNBOOKS_DIR / "ReconciliationRunFailed.md").read_text(encoding="utf-8")
    changed = text.replace(wrong, right, 1)
    assert changed != text
    with pytest.raises(runbooks.InvalidRunbook, match=why):
        runbooks.parse(changed, "ReconciliationRunFailed.md")


def failed_days(*rows):
    return {"days": [{"date": day, "failed_runs": count, "last_error": error, "last_failed_hours_ago": 0.5}
                     for day, count, error in rows], "days_read": 8}


def test_a_day_is_rerun_for_a_known_source_until_it_has_failed_three_times():
    sorted_ = reruns.sort(failed_days(("2026-10-11", 1, LEDGER_DOWN), ("2026-10-10", 2, ABANDONED),
                                      ("2026-10-09", 3, PROCESSOR_DOWN), ("2026-10-08", 1, "java.lang.IllegalStateException: boom")),
                          RUNBOOK)

    assert [(row["id"], row["cause"]) for row in sorted_["rerun"]] == [("2026-10-11", "ledger_unavailable"),
                                                                       ("2026-10-10", "run_abandoned")]
    assert [(row["id"], row["cause"]) for row in sorted_["needs_a_person"]] == [("2026-10-09", "processor_unavailable"),
                                                                                ("2026-10-08", None)]
    assert reruns.sort("PayFlow did not answer", RUNBOOK) == {"rerun": [], "needs_a_person": [], "unread": "PayFlow did not answer"}


def test_a_cause_that_no_sorted_day_has_cannot_stand():
    rows = [{"cause": "ledger_unavailable"}]
    assert any("not the cause of any of them" in why
               for why in diagnosis.unsupported({"root_cause": "processor_unavailable"}, [], {}, RUNBOOK, rows))
    assert not any("not the cause of any of them" in why
                   for why in diagnosis.unsupported({"root_cause": "ledger_unavailable"}, [], {}, RUNBOOK, rows))


# ---- the operator login: a day's runs, and running it again

def fake_payflow(runs_of_day, rerun_answer=None, status=201, slow=False):
    asked = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        asked.append((request.method, request.url.path, request.url.params.get("date") or request.content.decode()))
        if request.method == "GET":
            return httpx.Response(200, json=runs_of_day)
        if slow:
            raise httpx.ReadTimeout("still reconciling")
        return httpx.Response(status, json=rerun_answer or {"message": "That day is being reconciled"})
    login = Operator("http://payflow", "incident-agent-operator", "s3cret",
                     http=httpx.AsyncClient(base_url="http://payflow", transport=httpx.MockTransport(handle)))
    return login, asked


def test_the_operator_reads_a_days_runs_newest_first_and_runs_it_again():
    d11 = WEEK[0]
    login, asked = fake_payflow([run_of(d11, "FAILED", error=LEDGER_DOWN), run_of(d11, "FAILED", started="04:00:00")],
                                rerun_answer={"run": run_of(d11, discrepancies=0), "discrepancies": []})

    runs = asyncio.run(login.runs(d11.isoformat()))
    again = asyncio.run(login.rerun(d11.isoformat()))

    assert [item["started_at"] for item in runs] == [f"{TODAY}T04:00:00", f"{TODAY}T02:00:10"]
    assert again["status"] == "COMPLETED"
    assert asked[-1] == ("POST", "/api/v1/reconciliation/runs", '{"date":"' + d11.isoformat() + '"}')


def test_a_day_already_being_reconciled_is_refused_and_a_slow_one_is_watched():
    refused, _ = fake_payflow([], status=409)
    with pytest.raises(Refused, match="being reconciled"):
        asyncio.run(refused.rerun("2026-10-11"))

    slow, _ = fake_payflow([], slow=True)
    assert asyncio.run(slow.rerun("2026-10-11")) is None


# ---- one day, checked again and run again

class FakeOperator:
    def __init__(self, runs, rerun=None, fail=None):
        self.runs_of = runs
        self.rerun_answer = rerun
        self.fail = fail
        self.reran = []

    async def runs(self, day):
        if self.fail:
            raise self.fail
        return self.runs_of

    async def rerun(self, day):
        self.reran.append(day)
        return self.rerun_answer


def summary(status, error=None, discrepancies=None, started="2026-10-12T08:00:00"):
    return {"id": "r", "status": status, "started_at": started, "finished_at": None, "error": error, "discrepancies": discrepancies}


def day_row(cause="ledger_unavailable"):
    return {"id": "2026-10-11", "cause": cause, "failed_runs": 1, "error": LEDGER_DOWN}


def healthy_tools():
    return ChangingTools(get_pod_status=PODS_READY | {"workload": "ledger-service"}, get_service_health=HEALTHY)


def test_a_day_still_failed_with_its_source_healthy_is_run_again():
    operator = FakeOperator([summary("FAILED", LEDGER_DOWN)], rerun=summary("COMPLETED", discrepancies=0))
    tools = healthy_tools()
    done = asyncio.run(carry_out.rerun_one(day_row(), operator, tools))
    assert done["outcome"] == "rerun" and operator.reran == ["2026-10-11"]
    assert carry_out.run_settled(done["run"]) == "COMPLETED, clean"
    assert ("get_pod_status", {"workload": "ledger-service"}) in tools.asked      # the source that failed, not another


@pytest.mark.parametrize(("runs", "tools", "why"), [
    ([summary("COMPLETED"), summary("FAILED")], None, "reconciled meanwhile"),
    ([summary("RUNNING")], None, "a run of it is going"),
    ([summary("FAILED")], ChangingTools(get_pod_status=PODS_READY | {"pods_ready": 0}, get_service_health=HEALTHY),
     "ledger-service is not healthy yet"),
])
def test_a_day_reconciled_meanwhile_already_running_or_whose_source_is_still_down_is_skipped(runs, tools, why):
    operator = FakeOperator(runs, rerun=summary("COMPLETED"))
    done = asyncio.run(carry_out.rerun_one(day_row(), operator, tools or healthy_tools()))
    assert done["outcome"] == "skipped" and why in done["why"] and operator.reran == []


def test_payflow_away_is_recorded_for_that_day():
    done = asyncio.run(carry_out.rerun_one(day_row(), FakeOperator([], fail=OperatorUnavailable("no answer")), healthy_tools()))
    assert done["outcome"] == "not_reached"


# ---- the workflow: propose, approve, rerun, watch

RECONCILIATION = {"days": [], "concerns": [f"{WEEK[0]} is not reconciled: its 1 run(s) failed, the last with: {LEDGER_DOWN[:150]}"],
                  "detail": {"last_error": LEDGER_DOWN[:200]}}
LEDGER = {"root_cause": "ledger_unavailable", "confidence": "high", "summary": "The ledger did not answer the run.",
          "evidence": [{"check": "reconciliation", "quote": "http://ledger-service:8082/api/v1/ledger/transactions"}]}
AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def investigate(tmp_path, saver=None, diagnosis_=LEDGER, failed=None):
    tools = FakeTools([alert("ReconciliationRunFailed", severity="warning")], get_reconciliation=RECONCILIATION,
                      list_failed_reconciliations=failed or failed_days((WEEK[0].isoformat(), 1, LEDGER_DOWN)))
    return asyncio.run(run.investigate("ReconciliationRunFailed", tools, ScriptedModel(["reconciliation"], diagnoses=[diagnosis_]),
                                       tmp_path, now=lambda: AT, checkpointer=saver, ask_for_decision=saver is not None))


def test_the_proposal_carries_the_days_code_found_and_the_report_lists_them(tmp_path):
    trace = investigate(tmp_path, failed=failed_days((WEEK[0].isoformat(), 1, LEDGER_DOWN), (WEEK[1].isoformat(), 3, LEDGER_DOWN)))

    proposal = load(trace.path)["result"]["proposal"]
    assert proposal["cause"] == "ledger_unavailable" and [row["id"] for row in proposal["rerun"]] == [WEEK[0].isoformat()]
    report = trace.report_path.read_text(encoding="utf-8")
    assert "### Failed days" in report and "**To run again (1):**" in report and "**Needs a person (1):**" in report
    assert "runs a day's reconciliation again only when a person approves" in report


def test_an_approved_rerun_list_runs_each_day_and_watches_one_still_going(tmp_path):
    saver = InMemorySaver()
    trace = investigate(tmp_path, saver)
    operator = FakeOperator([summary("FAILED", LEDGER_DOWN)], rerun=None)     # still going after the wait
    reads = iter([[summary("FAILED", LEDGER_DOWN)], [summary("RUNNING")], [summary("COMPLETED", discrepancies=2)]])

    async def runs(day):
        return next(reads)
    operator.runs = runs
    clock = Clock()

    done = asyncio.run(run.resume(trace.path, {"decision": "approved", "by": "alice", "at": "2026-10-12T09:05:00Z", "note": ""},
                                  saver, now=lambda: AT, tools=healthy_tools(), operator=operator, sleep=clock.sleep, clock=clock))

    document = load(done.path)
    assert [step["step"] for step in document["steps"]][-3:] == ["decide", "rerun", "watch_runs"]
    assert [(item["outcome"], item["ended"]) for item in document["result"]["carried_out"]] == [("rerun", "COMPLETED, 2 discrepancy(ies)")]
    assert "1 of 1 were run again" in done.report_path.read_text(encoding="utf-8")
