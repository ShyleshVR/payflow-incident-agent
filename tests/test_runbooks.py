import asyncio
from datetime import UTC, datetime

import jsonschema
import pytest
from mcp import Client

from agent import config, run, runbooks
from agent.tools import ReplayTools
from agent.trace import load
from cli import payflow_agent
from mcp_server import server as mcp_server_module
from tests.fakes import FakeTools, ScriptedModel

AT = datetime(2026, 10, 12, 13, 26, 0, tzinfo=UTC)
CIRCUIT_OPEN = {"alert": "ProcessorCircuitOpen", "severity": "critical", "about": {"application": "payment-service"},
                "firing_for_minutes": 3, "summary": "The card processor circuit breaker is open"}

MINIMAL = """---
alert: {alert}
based_on: a test
causes:
  some_cause: What it means,
    in a line.
checks:
  processor_calls:
    tool: get_dependency_status
    arguments: {{minutes: 15}}
    tells: How the calls to the
      processor ended.
  sagas:
    tool: list_sagas_needing_attention
    tells: Which payments are parked.
---
# {alert}

What it means, in a sentence.

## What resolves it

It closes by itself.
Nothing to restart.

## Afterwards

Not part of the resolution.
"""


def Firing(*alerts):
    return FakeTools(alerts)


def test_the_processor_runbook_has_its_checks_for_the_code_and_its_guidance_for_the_model():
    runbook = runbooks.load("ProcessorCircuitOpen", config.RUNBOOKS_DIR)

    assert runbook.alert == "ProcessorCircuitOpen"
    assert "PayFlow docs/DEPLOYMENT.md" in runbook.based_on
    assert [check.name for check in runbook.checks] == [
        "processor_calls", "processor_pods", "processor_health", "processor_errors", "payment_service_errors",
        "payment_service_health", "database", "sagas"]
    assert runbook.checks[1] == runbooks.Check(
        name="processor_pods", tool="get_pod_status", arguments={"workload": "processor-simulator"},
        tells="Whether the processor's pods are running and ready, or crashing, unschedulable or missing.")
    assert [cause.name for cause in runbook.causes] == [
        "processor_outage", "processor_not_running", "processor_dependency_down", "processor_unreachable"]
    assert runbook.causes[1].description == "The processor's pods are missing, crashing, unschedulable or not ready."
    assert runbook.guidance.startswith("# ProcessorCircuitOpen")
    assert "payment-service is not getting answers from the processor" in runbook.guidance
    assert "---" not in runbook.guidance  # the header is not shown to the model


def test_every_check_of_every_runbook_is_a_real_tool_call_with_valid_arguments():
    async def schemas():
        async with Client(mcp_server_module.server) as client:
            return {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}

    tools = asyncio.run(schemas())
    names = runbooks.available(config.RUNBOOKS_DIR)

    assert "ProcessorCircuitOpen" in names
    # a check that names a label (ServiceDown's "{job}") is tried with each value the alert can carry
    label_values = {"job": ["payment-service", "ledger-service", "api-gateway", "processor-simulator"],
                    "application": ["payment-service", "payout-service"], "pod": ["payment-service-7d9c8b6f5-abcde"]}
    for name in names:
        loaded = runbooks.load(name, config.RUNBOOKS_DIR).as_dict()
        for value_index in range(4):
            about = {label: values[min(value_index, len(values) - 1)] for label, values in label_values.items()}
            for check in runbooks.resolve(loaded, [{"about": about}])["checks"]:
                assert check["tool"] in tools, f"{name}.md: check {check['name']} uses a tool that does not exist: {check['tool']}"
                # the tool's own schema decides: a service that does not exist, a missing argument, a wrong type
                jsonschema.validate(check["arguments"], tools[check["tool"]])


def test_the_schema_check_above_would_catch_a_service_that_does_not_exist():
    async def schema():
        async with Client(mcp_server_module.server) as client:
            return {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}["get_pod_status"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"workload": "billing-service"}, asyncio.run(schema()))


def test_a_runbook_is_found_by_the_alerts_exact_name_and_nothing_else(tmp_path):
    (tmp_path / "ProcessorCircuitOpen.md").write_text(MINIMAL.format(alert="ProcessorCircuitOpen"), encoding="utf-8")
    (tmp_path / "KafkaUnreachable.md").write_text(MINIMAL.format(alert="KafkaUnreachable"), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a runbook", encoding="utf-8")

    runbook = runbooks.load("KafkaUnreachable", tmp_path)

    assert runbook.checks[0].tells == "How the calls to the processor ended."   # one line, however it was wrapped
    assert runbook.causes == (runbooks.Cause("some_cause", "What it means, in a line."),)
    assert runbook.checks[1].arguments == {}
    assert runbooks.available(tmp_path) == ["KafkaUnreachable", "ProcessorCircuitOpen"]
    for unknown in ("KafkaConsumerLag", "processorcircuitopen", "../ProcessorCircuitOpen", "sub/ProcessorCircuitOpen",
                    "ProcessorCircuitOpen.md", ""):
        with pytest.raises(runbooks.NoRunbook) as refused:
            runbooks.load(unknown, tmp_path)
        assert str(refused.value) == f"No runbook for {unknown} yet. Runbooks exist for: KafkaUnreachable, ProcessorCircuitOpen."


def test_a_runbook_file_with_windows_line_endings_is_read_the_same(tmp_path):
    (tmp_path / "ProcessorCircuitOpen.md").write_bytes(MINIMAL.format(alert="ProcessorCircuitOpen").replace("\n", "\r\n").encode())

    assert [check.name for check in runbooks.load("ProcessorCircuitOpen", tmp_path).checks] == ["processor_calls", "sagas"]


@pytest.mark.parametrize(("text", "why"), [
    ("# ProcessorCircuitOpen\nNo header at all.\n", "must begin with a header"),
    (MINIMAL.format(alert="SomethingElse"), "the header says alert: SomethingElse"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks: {}\n---\nText.\n", "at least one check"),
    ("---\nalert: ProcessorCircuitOpen\nchecks:\n  a:\n    tool: get_saga\n    tells: x\n---\nText.\n", "at least one cause"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  unknown: Not known.\nchecks: {}\n---\nText.\n", "cause unknown must be a lower-case name"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  Processor Outage: It is down.\nchecks: {}\n---\nText.\n", "cause Processor Outage must be"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  processor_outage:\nchecks: {}\n---\nText.\n", "cause processor_outage needs a line"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks:\n  a:\n    tool: get_saga\n---\nText.\n", "check a needs a tool and a line saying what it tells"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks:\n  a:\n    tool: get_saga\n    tells: x\n    retries: 3\n---\nText.\n",
     "check a may only have tool, arguments and tells"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks:\n  a: [unclosed\n---\nText.\n", "not valid YAML"),
    ("---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks:\n  a:\n    tool: get_saga\n    tells: x\n---\n\n", "no guidance"),
])
def test_a_runbook_file_the_workflow_must_not_run_with_is_refused_with_its_name(text, why):
    with pytest.raises(runbooks.InvalidRunbook, match="ProcessorCircuitOpen.md: ") as refused:
        runbooks.parse(text, "ProcessorCircuitOpen.md")
    assert why in str(refused.value)


def test_the_runbook_as_it_was_used_is_in_the_trace_and_the_report(tmp_path):
    trace = asyncio.run(run.investigate("ProcessorCircuitOpen", Firing(CIRCUIT_OPEN), ScriptedModel(), tmp_path, now=lambda: AT))

    document = load(trace.path)
    assert document["runbook"]["alert"] == "ProcessorCircuitOpen"
    assert document["runbook"]["checks"][0] == {
        "name": "processor_calls", "tool": "get_dependency_status", "arguments": {"minutes": 15},
        "tells": "How payment-service's calls to the card processor ended in the last 15 minutes (answered or not, per "
                 "operation), and the circuit breaker's state."}
    assert document["runbook"]["causes"][0]["name"] == "processor_outage"
    assert document["runbook"]["guidance"].startswith("# ProcessorCircuitOpen")
    assert document["steps"][1]["step"] == "load_runbook"
    assert document["steps"][1]["output"] == {"runbook": "ProcessorCircuitOpen.md", "checks": document["result"]["checks_available"]}
    assert len(document["result"]["checks_available"]) == 8


def test_an_alert_without_a_runbook_is_refused_before_anything_is_asked_or_written(tmp_path, monkeypatch, capsys):
    tools = Firing({**CIRCUIT_OPEN, "alert": "NoSuchAlert"})

    with pytest.raises(runbooks.NoRunbook, match="No runbook for NoSuchAlert yet"):
        asyncio.run(run.investigate("NoSuchAlert", tools, ScriptedModel(), tmp_path))

    assert tools.asked == []
    assert list(tmp_path.iterdir()) == []

    async def refused(alert_name, mcp_url, runs_dir):
        return await run.investigate(alert_name, tools, ScriptedModel(), runs_dir)

    monkeypatch.setattr(payflow_agent, "investigate_live", refused)
    assert payflow_agent.main(["investigate", "NoSuchAlert"]) == payflow_agent.NO_RUNBOOK
    known = ", ".join(runbooks.available(config.RUNBOOKS_DIR))
    assert f"No runbook for NoSuchAlert yet. Runbooks exist for: {known}." in capsys.readouterr().out


def test_a_replay_runs_with_the_runbook_as_it_is_now_not_as_it_was_recorded(tmp_path):
    then, now = tmp_path / "then", tmp_path / "now"
    for directory, text in ((then, MINIMAL.format(alert="ProcessorCircuitOpen")),
                            (now, MINIMAL.format(alert="ProcessorCircuitOpen").replace("sagas:", "parked_payments:"))):
        directory.mkdir()
        (directory / "ProcessorCircuitOpen.md").write_text(text, encoding="utf-8")
    recorded = asyncio.run(run.investigate("ProcessorCircuitOpen", Firing(CIRCUIT_OPEN), ScriptedModel(), tmp_path / "runs",
                                           runbooks_dir=then, now=lambda: AT))

    replayed = asyncio.run(run.investigate("ProcessorCircuitOpen", ReplayTools(load(recorded.path)), ScriptedModel(),
                                           tmp_path / "replays", replay_of=recorded.id, runbooks_dir=now))

    assert recorded.document["result"]["checks_available"] == ["processor_calls", "sagas"]
    assert replayed.document["result"]["checks_available"] == ["processor_calls", "parked_payments"]


def test_a_runbook_says_which_checks_can_show_each_cause():
    loaded = runbooks.load("ProcessorCircuitOpen", config.RUNBOOKS_DIR)

    shown_by = {cause.name: cause.shown_by for cause in loaded.causes}
    assert shown_by == {"processor_outage": ("processor_errors", "processor_health"),
                        "processor_not_running": ("processor_pods", "processor_health"),
                        "processor_dependency_down": ("processor_errors", "processor_health", "database"),
                        "processor_unreachable": ("payment_service_errors",)}
    assert list(loaded.as_dict()["causes"][0]["shown_by"]) == ["processor_errors", "processor_health"]
    # and which must have run and found nothing
    assert {cause.name: cause.requires_clear for cause in loaded.causes if cause.requires_clear} == {
        "processor_unreachable": ("processor_pods", "processor_health")}


def test_shown_by_may_only_name_the_runbooks_own_causes_and_checks():
    text = (config.RUNBOOKS_DIR / "ProcessorCircuitOpen.md").read_text(encoding="utf-8")
    for wrong, why in (("  processor_outage: [processor_errors, processor_health]", "  cosmic_rays: [processor_errors]"),
                       ("  processor_outage: [processor_errors, processor_health]", "  processor_outage: [vibes]"),
                       ("  processor_outage: [processor_errors, processor_health]", "  processor_outage: []"),
                       ("  processor_unreachable: [processor_pods, processor_health]", "  processor_unreachable: [vibes]")):
        changed = text.replace(wrong, why)
        assert changed != text
        with pytest.raises(runbooks.InvalidRunbook, match="shown_by|requires_clear"):
            runbooks.parse(changed, "ProcessorCircuitOpen.md")
