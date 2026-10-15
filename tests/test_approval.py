import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent import run
from agent.trace import load
from api import store as states
from api.app import create_app
from api.service import DecisionRefused, Service, UnknownInvestigation
from api.store import Store
from cli import payflow_agent
from tests.fakes import CIRCUIT_OPEN, FakeTools, ScriptedModel

AT = datetime(2026, 10, 15, 10, 1, 0, tzinfo=UTC)
OUTAGE = {"root_cause": "processor_outage", "confidence": "high", "summary": "The processor was switched off on its side.",
          "evidence": [{"check": "processor_errors", "quote": "Simulated outage set to 240s"}]}
ERRORS = {"workload": "processor-simulator", "error_lines": 0, "warning_lines": 1, "concerns": [],
          "groups": [{"level": "WARN", "logger": "c.s.p.web.ProcessorController", "message": "Simulated outage set to 240s", "count": 1}]}
APPROVED = {"decision": "approved", "by": "alice", "at": "2026-10-15T10:05:00Z", "note": "called the processor"}


def investigate(tmp_path, diagnosis=OUTAGE, **options):
    tools = FakeTools([CIRCUIT_OPEN], get_recent_errors=ERRORS)
    model = ScriptedModel(["processor_errors"], diagnoses=[diagnosis] if diagnosis else [])
    return asyncio.run(run.investigate("ProcessorCircuitOpen", tools, model, tmp_path, now=lambda: AT, **options))


def steps(trace):
    return [step["step"] for step in load(trace.path)["steps"]]


# ---- the workflow

def test_in_the_service_the_workflow_pauses_at_its_proposal_with_the_report_already_written(tmp_path):
    trace = investigate(tmp_path, checkpointer=InMemorySaver(), ask_for_decision=True)

    assert trace.document["status"] == "awaiting_decision"
    assert steps(trace)[-3:] == ["screen", "propose", "report"]
    proposal = trace.document["result"]["proposal"]
    assert proposal["cause"] == "processor_outage" and proposal["step"].startswith("Contact the card processor about the outage")
    assert trace.document["result"]["decision"] is None
    text = trace.report_path.read_text(encoding="utf-8")
    assert "### Proposed next step" in text and "**Waiting for a decision.**" in text and proposal["step"] in text


def test_a_decision_resumes_the_workflow_which_records_it_in_the_trace_and_the_report(tmp_path):
    saver = InMemorySaver()
    paused = investigate(tmp_path, checkpointer=saver, ask_for_decision=True)

    done = asyncio.run(run.resume(paused.path, APPROVED, saver))

    assert done.document["status"] == "completed" and done.id == paused.id
    assert steps(done)[-2:] == ["report", "decide"]
    assert done.document["result"]["decision"] == APPROVED
    text = done.report_path.read_text(encoding="utf-8")
    assert '**Approved** by alice at 2026-10-15 10:05:00 UTC: "called the processor"' in text
    assert "Nothing was carried out: the agent does not act yet" in text
    with pytest.raises(ValueError, match="not waiting for a decision"):
        asyncio.run(run.resume(paused.path, APPROVED, saver))       # decided already


def test_a_paused_workflow_is_resumed_after_a_restart_from_its_saved_state(tmp_path):
    database = tmp_path / "checkpoints.sqlite"

    async def before_the_restart():
        async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
            tools = FakeTools([CIRCUIT_OPEN], get_recent_errors=ERRORS)
            return await run.investigate("ProcessorCircuitOpen", tools, ScriptedModel(["processor_errors"], diagnoses=[OUTAGE]),
                                         tmp_path, now=lambda: AT, checkpointer=saver, ask_for_decision=True)

    async def after_the_restart(path):
        async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:   # a new connection, as in a new process
            return await run.resume(path, APPROVED | {"decision": "rejected", "note": "it came back by itself"}, saver)

    paused = asyncio.run(before_the_restart())
    done = asyncio.run(after_the_restart(paused.path))

    assert (done.document["status"], done.document["result"]["decision"]["decision"]) == ("completed", "rejected")
    assert '**Rejected** by alice at 2026-10-15 10:05:00 UTC: "it came back by itself"' in done.report_path.read_text(encoding="utf-8")


def test_nothing_waits_when_there_is_nothing_to_propose_or_nobody_to_ask(tmp_path):
    unknown = investigate(tmp_path / "unknown", diagnosis=None, checkpointer=InMemorySaver(), ask_for_decision=True)
    assert (unknown.document["status"], unknown.document["result"]["proposal"]) == ("completed", None)
    assert "Proposed next step" not in unknown.report_path.read_text(encoding="utf-8")

    outside = investigate(tmp_path / "outside")                     # the command line, an eval
    assert outside.document["status"] == "completed" and outside.document["result"]["proposal"]["cause"] == "processor_outage"
    assert "Not asked: this investigation was run outside the agent's service" in outside.report_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="needs a checkpointer"):
        investigate(tmp_path / "x", ask_for_decision=True)


# ---- the service

def service(tmp_path, **options):
    saver = InMemorySaver()

    async def investigate_one(alert_name):
        tools = FakeTools([CIRCUIT_OPEN], get_recent_errors=ERRORS)
        return await run.investigate(alert_name, tools, ScriptedModel(["processor_errors"], diagnoses=[OUTAGE]), tmp_path,
                                     checkpointer=saver, ask_for_decision=True)

    async def resume(trace_id, decision):
        return await run.resume(tmp_path / f"{trace_id}.json", decision, saver)

    return Service(Store(tmp_path / "agent.sqlite"), investigate_one, lambda name: name == "ProcessorCircuitOpen", resume=resume,
                   **options)


def alert(fingerprint="f1", status="firing", starts_at="2026-10-15T10:00:00Z"):
    return {"status": status, "labels": {"alertname": "ProcessorCircuitOpen", "severity": "critical"}, "annotations": {},
            "startsAt": starts_at, "endsAt": "2026-10-15T10:20:00Z", "fingerprint": fingerprint}


def notification(*alerts):
    return {"version": "4", "status": "firing", "receiver": "incident-agent", "alerts": list(alerts)}


def waiting_service(tmp_path):
    agent = service(tmp_path)
    agent.receive(notification(alert("f1")))
    asyncio.run(agent.process_one())
    return agent


def test_an_investigation_with_a_proposal_waits_and_a_decision_completes_it(tmp_path):
    agent = waiting_service(tmp_path)
    waiting = agent.store.investigation(1)
    assert (waiting["status"], waiting["root_cause"]) == ("awaiting_decision", "processor_outage")
    assert waiting["proposal"].startswith("Contact the card processor") and waiting["decision"] is None
    assert [item["id"] for item in agent.store.investigations(status="awaiting_decision")] == [1]

    decided = asyncio.run(agent.decide(1, states.APPROVED, by="alice", note="  done  "))

    assert (decided["status"], decided["decision"], decided["decided_by"], decided["decision_note"]) == (
        "completed", "approved", "alice", "done")
    assert load(tmp_path / f"{decided['trace_id']}.json")["result"]["decision"]["by"] == "alice"
    for wrong, why in (((1, states.REJECTED, "bob", "late"), "is completed, not waiting for a decision"),
                       ((1, "maybe", "bob", ""), "approved or rejected")):
        with pytest.raises(DecisionRefused, match=why):
            asyncio.run(agent.decide(*wrong))
    with pytest.raises(UnknownInvestigation):
        asyncio.run(agent.decide(9, states.APPROVED, "bob"))


def test_a_rejection_needs_a_reason(tmp_path):
    agent = waiting_service(tmp_path)

    with pytest.raises(DecisionRefused, match="a rejection needs a reason"):
        asyncio.run(agent.decide(1, states.REJECTED, by="bob", note=" "))
    assert agent.store.investigation(1)["status"] == "awaiting_decision"


def test_a_proposal_lapses_once_every_firing_behind_it_has_resolved(tmp_path):
    agent = waiting_service(tmp_path)
    joined = agent.receive(notification(alert("f2")))                       # the same alarm, another instance: joins
    assert joined["joined"] == [1]

    one = asyncio.run(agent.handle(notification(alert("f1", status="resolved"))))
    assert (one["lapsing"], agent.store.investigation(1)["status"]) == ([], "awaiting_decision")   # f2 still fires

    both = asyncio.run(agent.handle(notification(alert("f2", status="resolved"))))

    lapsed = agent.store.investigation(1)
    assert both["lapsing"] == [1]
    assert (lapsed["status"], lapsed["decision"], lapsed["decided_by"]) == ("completed", "lapsed", "incident agent")
    text = (tmp_path / f"{lapsed['trace_id']}.md").read_text(encoding="utf-8")
    assert "every alarm behind it resolved before anyone decided" in text


# ---- over HTTP, and from the command line

def test_a_decision_is_posted_and_the_waiting_ones_can_be_listed(tmp_path):
    agent = waiting_service(tmp_path)
    with TestClient(create_app(agent, tmp_path)) as client:
        assert [item["id"] for item in client.get("/investigations", params={"status": "awaiting_decision"}).json()] == [1]
        assert client.post("/investigations/1/decision", json={"decision": "reject", "by": "bob"}).status_code == 400
        assert client.post("/investigations/1/decision", json={"decision": "maybe"}).status_code == 422
        assert client.post("/investigations/9/decision", json={"decision": "approve"}).status_code == 404

        approved = client.post("/investigations/1/decision", json={"decision": "approve", "by": "bob", "note": "ok"})

        assert approved.status_code == 200 and approved.json()["decision"] == "approved"
        assert client.post("/investigations/1/decision", json={"decision": "approve"}).status_code == 409
        assert client.get("/investigations", params={"status": "awaiting_decision"}).json() == []


def test_the_command_line_lists_approves_and_rejects_through_the_service(tmp_path, monkeypatch, capsys):
    agent = waiting_service(tmp_path)
    agent.receive(notification(alert("g1", starts_at="2026-10-15T11:00:00Z")))   # joins 1, still waiting
    monkeypatch.setattr(payflow_agent, "_agent_client", lambda: TestClient(create_app(agent, tmp_path)))
    monkeypatch.setattr(payflow_agent.getpass, "getuser", lambda: "carol")

    assert payflow_agent.main(["pending"]) == payflow_agent.DONE
    listed = capsys.readouterr().out
    assert "   1  ProcessorCircuitOpen: processor_outage" in listed and "proposed: Contact the card processor" in listed

    assert payflow_agent.main(["approve", "1", "--note", "on it"]) == payflow_agent.DONE
    said = capsys.readouterr().out
    assert "Investigation 1 (ProcessorCircuitOpen: processor_outage): approved by carol" in said
    assert "Nothing is carried out: the agent does not act yet." in said
    assert payflow_agent.main(["reject", "1", "--reason", "too late"]) == payflow_agent.FAILED
    assert "is completed, not waiting for a decision" in capsys.readouterr().out
    assert payflow_agent.main(["pending"]) == payflow_agent.DONE and "Nothing waits for a decision." in capsys.readouterr().out
    with pytest.raises(SystemExit):
        payflow_agent.main(["reject", "1"])                                      # a reason is required


def test_the_command_line_says_so_when_the_service_is_not_running(monkeypatch, capsys):
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(payflow_agent, "_agent_client", lambda: httpx.Client(transport=httpx.MockTransport(refused),
                                                                           base_url="http://127.0.0.1:8092"))

    assert payflow_agent.main(["pending"]) == payflow_agent.FAILED
    assert "The agent service does not answer on port 8092. Start it with: payflow-agent serve" in capsys.readouterr().out


def test_an_earlier_database_gets_the_decision_columns(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "agent.sqlite")
    old.executescript("CREATE TABLE investigations (id INTEGER PRIMARY KEY AUTOINCREMENT, alert_name TEXT NOT NULL, "
                      "status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, trace_id TEXT, "
                      "root_cause TEXT, error TEXT);")
    old.execute("INSERT INTO investigations (alert_name, status, created_at) VALUES ('ProcessorCircuitOpen', 'completed', 'x')")
    old.commit()
    old.close()

    store = Store(tmp_path / "agent.sqlite")

    assert store.investigation(1)["decision"] is None and json.dumps(store.investigation(1))
