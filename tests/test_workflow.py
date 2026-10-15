import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mcp import Client

from agent import run
from agent.graph import InvestigationError
from agent.tools import LiveTools, ReplayMismatch, ReplayTools, ToolResult
from agent.trace import Trace, load
from cli import payflow_agent
from mcp_server import server as mcp_server_module
from mcp_server.clients import alertmanager
from tests.fakes import CIRCUIT_OPEN, FakeTools, ScriptedModel, alert

AT = datetime(2026, 10, 12, 13, 26, 0, tzinfo=UTC)


def firing(*alerts):
    return FakeTools(alerts)


def investigate(tools, tmp_path, name="ProcessorCircuitOpen", model=None, **options):
    return asyncio.run(run.investigate(name, tools, model or ScriptedModel(), tmp_path, now=lambda: AT, **options))


def test_a_firing_alert_is_taken_in_and_reported_and_every_step_is_in_the_trace_file(tmp_path):
    tools = firing(alert("PayoutsFailing", "warning"), CIRCUIT_OPEN)

    trace = investigate(tools, tmp_path)

    assert trace.path == tmp_path / "20261012T132600Z-ProcessorCircuitOpen.json"
    document = load(trace.path)
    assert (document["id"], document["status"], document["replay_of"]) == ("20261012T132600Z-ProcessorCircuitOpen", "completed", None)
    assert (document["started_at"], document["finished_at"]) == ("2026-10-12T13:26:00Z", "2026-10-12T13:26:00Z")
    assert document["alerts"] == [CIRCUIT_OPEN]
    # the scripted model asked for nothing: no rounds, but every step is there
    assert [step["step"] for step in document["steps"]] == ["intake", "load_runbook", "choose_checks", "record_unchosen", "diagnose",
                                                            "screen", "report"]
    intake = document["steps"][0]
    assert intake["output"] == {"firing_with_this_name": 1, "all_firing": ["PayoutsFailing", "ProcessorCircuitOpen"]}
    assert [(call["tool"], call["arguments"], call["is_error"]) for call in intake["tool_calls"]] == [("list_active_alerts", {}, False)]
    assert intake["tool_calls"][0]["result"]["alerts"][1] == CIRCUIT_OPEN
    assert document["result"] == document["steps"][-1]["output"]
    assert {key: document["result"][key] for key in ("alert", "severity", "firing_for_minutes", "affected")} == {
        "alert": "ProcessorCircuitOpen", "severity": "critical", "firing_for_minutes": 3,
        "affected": [{"application": "payment-service"}]}
    # with no check run there is nothing to diagnose from, and the model is not even asked
    assert (document["result"]["diagnosis"]["root_cause"], document["result"]["diagnosis"]["attempts"]) == ("unknown", 0)
    assert all(isinstance(step["seconds"], float) for step in document["steps"])


def test_alerts_with_one_name_and_different_labels_are_one_investigation(tmp_path):
    tools = firing(alert("DeadLettersArriving", "warning", 12, topic="webhook-deliveries.DLT"),
                   alert("DeadLettersArriving", "warning", 4, topic="payment-created.DLT"), CIRCUIT_OPEN)
    books = tmp_path / "runbooks"
    books.mkdir()
    (books / "DeadLettersArriving.md").write_text(
        "---\nalert: DeadLettersArriving\ncauses:\n  some_cause: What it means.\nchecks:\n  messaging:\n    tool: get_messaging_status\n"
        "    tells: Which dead-letter topics received messages.\n---\nA consumer gave up on a message.\n\n## What resolves it\nIt recovers by itself.\n", encoding="utf-8")

    trace = investigate(tools, tmp_path / "runs", "DeadLettersArriving", runbooks_dir=books)

    assert len(list((tmp_path / "runs").glob("*.json"))) == 1
    assert trace.document["result"]["affected"] == [{"topic": "webhook-deliveries.DLT"}, {"topic": "payment-created.DLT"}]
    assert trace.document["result"]["firing_for_minutes"] == 12


def test_an_alert_that_is_not_firing_is_not_an_investigation_and_leaves_no_file(tmp_path):
    with pytest.raises(run.NotFiring) as refused:
        investigate(firing(alert("PayoutsFailing", "warning")), tmp_path)

    assert str(refused.value) == "ProcessorCircuitOpen is not firing. Firing now: PayoutsFailing."
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(run.NotFiring, match="Firing now: nothing."):
        investigate(firing(), tmp_path)


def test_alerts_that_cannot_be_read_stop_the_investigation_without_a_file(tmp_path):
    tools = FakeTools(list_active_alerts=ToolResult(True, "Alertmanager did not answer at http://localhost:9093"))

    with pytest.raises(InvestigationError, match="Alertmanager did not answer"):
        investigate(tools, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_recorded_investigation_can_be_run_again_without_any_live_tool(tmp_path):
    recorded = investigate(firing(CIRCUIT_OPEN, alert("PayoutsFailing", "warning")), tmp_path / "live")
    later = datetime(2026, 10, 14, 8, 0, 5, tzinfo=UTC)

    replayed = asyncio.run(run.investigate("ProcessorCircuitOpen", ReplayTools(load(recorded.path)), ScriptedModel(),
                                           tmp_path / "replays", replay_of=recorded.id, now=lambda: later))

    assert replayed.id == "20261014T080005Z-ProcessorCircuitOpen"
    assert replayed.document["replay_of"] == "20261012T132600Z-ProcessorCircuitOpen"
    assert replayed.document["result"] == recorded.document["result"]
    # the replay's own trace holds the same tool results, so it can be replayed in turn
    assert load(replayed.path)["steps"][0]["tool_calls"][0]["result"] == load(recorded.path)["steps"][0]["tool_calls"][0]["result"]


def test_a_replay_hands_out_each_recorded_result_once_and_never_invents_one():
    recorded = {"steps": [{"tool_calls": [
        {"tool": "get_service_health", "arguments": {"service": "payment-service"}, "is_error": False, "result": {"n": 1}},
        {"tool": "get_dependency_status", "arguments": {"minutes": 15}, "is_error": False, "result": {"n": 2}},
        {"tool": "get_service_health", "arguments": {"service": "payment-service"}, "is_error": True, "result": "Prometheus did not answer"},
    ]}]}
    tools = ReplayTools(recorded)

    async def calls():
        # asked in another order than recorded: matched by tool and arguments
        first = await tools.call("get_dependency_status", {"minutes": 15})
        second = await tools.call("get_service_health", {"service": "payment-service"})
        third = await tools.call("get_service_health", {"service": "payment-service"})
        return first, second, third

    first, second, third = asyncio.run(calls())

    assert (first.data, second.data, third) == ({"n": 2}, {"n": 1}, ToolResult(True, "Prometheus did not answer"))
    for tool, arguments in (("get_service_health", {"service": "payment-service"}), ("get_service_health", {"service": "ledger-service"}),
                            ("get_pod_status", {"workload": "kafka"})):
        with pytest.raises(ReplayMismatch, match=tool):
            asyncio.run(tools.call(tool, arguments))


def test_a_step_that_crashes_leaves_the_steps_before_it_and_its_error_in_the_file(tmp_path):
    trace = Trace("ProcessorCircuitOpen", tmp_path, now=lambda: AT)
    trace.confirm([CIRCUIT_OPEN])

    async def two_steps():
        async with trace.step("intake") as step:
            step["output"] = {"firing_with_this_name": 1}
        async with trace.step("gather"):
            trace.tool_call("get_dependency_status", {"minutes": 15}, 0.3, False, {"concerns": []})
            raise RuntimeError("the model server went away")

    with pytest.raises(RuntimeError):
        asyncio.run(two_steps())

    document = json.loads(trace.path.read_text(encoding="utf-8"))  # a complete document, although the run died
    assert document["status"] == "running"
    assert [(step["step"], step.get("error")) for step in document["steps"]] == [
        ("intake", None), ("gather", "RuntimeError: the model server went away")]
    assert document["steps"][1]["tool_calls"][0]["tool"] == "get_dependency_status"
    trace.finish("failed", error="RuntimeError: the model server went away")
    assert load(trace.path)["status"] == "failed"
    assert not list(tmp_path.glob("*.partial"))


def test_live_tools_go_through_the_real_mcp_server(tmp_path, monkeypatch):
    async def firing_alerts():
        return [{"labels": {"alertname": "ProcessorCircuitOpen", "severity": "critical", "application": "payment-service"},
                 "annotations": {"summary": "payment-service has opened the processor circuit breaker"},
                 "startsAt": "2026-10-12T13:20:00Z", "status": {"state": "active"}, "fingerprint": "abc"}]

    monkeypatch.setattr(alertmanager, "firing_alerts", firing_alerts)

    # a runbook whose only check is the alert list itself, so nothing but Alertmanager (faked) is asked
    books = tmp_path / "runbooks"
    books.mkdir()
    (books / "ProcessorCircuitOpen.md").write_text(
        "---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks:\n  alerts:\n    tool: list_active_alerts\n    tells: What is firing.\n---\nText.\n\n## What resolves it\nIt recovers by itself.\n",
        encoding="utf-8")

    async def live():
        async with Client(mcp_server_module.server) as client:
            return await run.investigate("ProcessorCircuitOpen", LiveTools(client), ScriptedModel(["alerts"]), tmp_path / "runs",
                                         runbooks_dir=books)

    trace = asyncio.run(live())

    assert trace.document["status"] == "completed"
    assert trace.document["alerts"][0]["about"] == {"application": "payment-service"}
    assert trace.document["alerts"][0]["summary"] == "payment-service has opened the processor circuit breaker"


def test_the_workflows_own_errors_leave_the_mcp_connection_unwrapped(tmp_path, monkeypatch):
    class ClientOverHttp:
        """Like the real client over HTTP: whatever leaves its block comes out in an ExceptionGroup."""

        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, kind, error, traceback):
            if error is not None:
                raise ExceptionGroup("unhandled errors in a TaskGroup", [error])

        async def call_tool(self, tool, arguments):
            return SimpleNamespace(is_error=False, structured_content={"count": 0, "alerts": []})

    monkeypatch.setattr(run, "Client", ClientOverHttp)
    monkeypatch.setattr(run, "local_model", ScriptedModel)

    with pytest.raises(run.NotFiring, match="ProcessorCircuitOpen is not firing. Firing now: nothing."):
        asyncio.run(run.investigate_live("ProcessorCircuitOpen", "http://mcp", tmp_path))


def test_the_command_prints_the_result_and_says_when_nothing_is_firing_or_the_server_is_away(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(payflow_agent.config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(run, "local_model", ScriptedModel)
    recorded = investigate(firing(CIRCUIT_OPEN), tmp_path)

    assert payflow_agent.main(["investigate", "--replay", str(recorded.path)]) == payflow_agent.DONE
    printed = capsys.readouterr().out
    assert "(a replay of `20261012T132600Z-ProcessorCircuitOpen`)" in printed
    assert "| Alert | ProcessorCircuitOpen, critical, firing for 3 minutes when investigated |" in printed
    assert "| Affected | application=payment-service |" in printed
    assert str(tmp_path) in printed

    async def not_firing(alert_name, mcp_url, runs_dir):
        raise run.NotFiring(alert_name, ["PayoutsFailing"])

    monkeypatch.setattr(payflow_agent, "investigate_live", not_firing)
    assert payflow_agent.main(["investigate", "KafkaUnreachable"]) == payflow_agent.NOT_FIRING
    assert "KafkaUnreachable is not firing. Firing now: PayoutsFailing." in capsys.readouterr().out

    monkeypatch.undo()  # the real investigate_live again
    monkeypatch.setattr(run, "local_model", ScriptedModel)
    monkeypatch.setattr(payflow_agent.config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(payflow_agent.config, "MCP_URL", "http://127.0.0.1:9/mcp")  # nothing listens there
    assert payflow_agent.main(["investigate", "ProcessorCircuitOpen"]) == payflow_agent.FAILED
    assert "The MCP server does not answer at http://127.0.0.1:9/mcp" in capsys.readouterr().out

    assert payflow_agent.main(["investigate", "--replay", str(tmp_path / "missing.json")]) == payflow_agent.FAILED
    with pytest.raises(SystemExit):
        payflow_agent.main(["investigate"])
