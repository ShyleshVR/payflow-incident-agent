import asyncio
from datetime import UTC, datetime

import pytest

from agent import report, run, runbooks
from agent.llm import ModelUnavailable
from agent.tools import ReplayTools
from agent.trace import load
from cli import payflow_agent
from tests.fakes import CIRCUIT_OPEN, FakeTools, ScriptedModel

AT = datetime(2026, 10, 12, 13, 26, 0, tzinfo=UTC)
CALLS = {"processor": {"unanswered_percent": 91.5},
         "concerns": ["the processor circuit breaker is open on 2 of 2 payment-service instance(s)",
                      "3400 of 3715 calls to the processor (91.5%) got no answer to trust"]}
ERRORS = {"groups": [{"level": "WARN", "message": "Simulated outage set to 240s"}], "concerns": []}
SAGAS = {"payments_parked": 3, "concerns": ["3 payment saga(s) are parked and wait for an operator's retry; the oldest, pay_6e3b, for 10 minutes in CAPTURING"]}
OUTAGE = {"root_cause": "processor_outage", "confidence": "high",
          "evidence": [{"check": "processor_errors", "quote": "Simulated outage set to 240s"}],
          "summary": "The processor was switched off on its side."}


class Named(ScriptedModel):
    name = "qwen3.5-9b"


def investigate(tmp_path, diagnoses=(OUTAGE,), rounds=(["processor_calls", "processor_errors"], ["sagas"]), at=AT, **options):
    tools = FakeTools([CIRCUIT_OPEN], get_dependency_status=CALLS, get_recent_errors=ERRORS, list_sagas_needing_attention=SAGAS)
    return asyncio.run(run.investigate("ProcessorCircuitOpen", tools, Named(*rounds, diagnoses=diagnoses), tmp_path,
                                       now=lambda: at, **options))


def test_the_report_is_a_markdown_file_beside_the_trace_made_from_the_trace_alone(tmp_path):
    trace = investigate(tmp_path)

    assert trace.report_path == tmp_path / "20261012T132600Z-ProcessorCircuitOpen.md"
    text = trace.report_path.read_text(encoding="utf-8")
    assert text == report.render(load(trace.path))     # nothing in it that the trace does not hold
    assert text.startswith("# ProcessorCircuitOpen: processor_outage\n")
    assert [line for line in text.splitlines() if line.startswith("## ")] == [
        "## Diagnosis", "## What was checked", "## What to do", "## How this was produced"]


def test_the_report_states_the_alert_the_cause_and_the_verified_quotes(tmp_path):
    text = investigate(tmp_path).report_path.read_text(encoding="utf-8")

    assert "| Investigation | `20261012T132600Z-ProcessorCircuitOpen` |" in text
    assert "| Alert | ProcessorCircuitOpen, critical, firing for 3 minutes when investigated |" in text
    assert "| Affected | application=payment-service |" in text
    assert "| Investigated | 2026-10-12 13:26:00 UTC, in " in text
    # the cause, with the runbook's description of it; the model's own sentence marked as such
    assert ("**`processor_outage`**, high confidence. The processor is failing or switched off on its side. Its pods are "
            "running, but it answers with 5xx, or its own log says an outage began.") in text
    assert 'The model\'s summary: "The processor was switched off on its side."' in text
    assert "Each quote below was found in the result of the check it names." in text
    assert '| `processor_errors` | "Simulated outage set to 240s" |' in text
    assert "Cited but not found" not in text


def test_the_report_lists_each_check_what_it_found_and_what_was_not_looked_at(tmp_path):
    text = investigate(tmp_path).report_path.read_text(encoding="utf-8")

    assert ("| `processor_calls` | `get_dependency_status` | the processor circuit breaker is open on 2 of 2 payment-service "
            "instance(s); 3400 of 3715 calls to the processor (91.5%) got no answer to trust |") in text
    assert "| `processor_errors` | `get_recent_errors` | nothing of concern |" in text
    assert "The model chose these in 2 rounds; gathering stopped because the model had seen enough." in text
    assert ("Not looked at: `processor_pods`, `processor_health`, `payment_service_errors`, `payment_service_health`, "
            "`database`.") in text


def test_what_to_do_is_the_runbooks_own_words_plus_the_sagas_the_checks_found_waiting(tmp_path):
    text = investigate(tmp_path).report_path.read_text(encoding="utf-8")

    assert "This agent takes no action. From the runbook:" in text
    assert "> The breaker closes by itself as soon as trial calls succeed. Nothing in PayFlow needs to be" in text
    assert "> afterwards any saga that parked during the outage." in text
    assert "## What resolves it" not in text      # the section's heading belongs to the runbook, not the report
    assert ("Sagas waiting for an operator, according to the checks:\n\n- 3 payment saga(s) are parked and wait for an "
            "operator's retry; the oldest, pay_6e3b, for 10 minutes in CAPTURING") in text
    # without that check, or with nothing parked, the line is not there
    without = investigate(tmp_path / "b", rounds=(["processor_errors"],)).report_path.read_text(encoding="utf-8")
    assert "Sagas waiting" not in without


def test_the_report_says_how_it_was_produced(tmp_path):
    text = investigate(tmp_path).report_path.read_text(encoding="utf-8")

    assert '- Runbook: `ProcessorCircuitOpen.md` (based on PayFlow docs/DEPLOYMENT.md, section "Runbooks", ProcessorCircuitOpen)' in text
    # 3 calls choosing checks (900 in, 40 out each) and 1 diagnosing (1,800 in, 200 out)
    assert "- Model: qwen3.5-9b, 4 calls, 4,500 tokens in and 320 out" in text
    assert "- Tools: 9 calls" in text and "replayed" not in text
    assert "- Diagnosis: 1 attempt" in text
    assert "- Full record: `20261012T132600Z-ProcessorCircuitOpen.json`" in text


def test_quotes_that_were_not_found_are_shown_apart_and_an_unsupported_cause_is_not_reported(tmp_path):
    sloppy = OUTAGE | {"evidence": [*OUTAGE["evidence"], {"check": "processor_calls", "quote": "error rate 97%"}]}
    invented = {"root_cause": "processor_not_running", "confidence": "high", "summary": "The pods crashed.",
                "evidence": [{"check": "processor_calls", "quote": "pods are in CrashLoopBackOff"}]}

    flagged = investigate(tmp_path / "a", diagnoses=(sloppy, sloppy)).report_path.read_text(encoding="utf-8")
    refused = investigate(tmp_path / "b", diagnoses=(invented, invented)).report_path.read_text(encoding="utf-8")

    assert "### Cited but not found" in flagged and '| `processor_calls` | "error rate 97%" |' in flagged
    assert "- Diagnosis: 2 attempts" in flagged
    assert refused.startswith("# ProcessorCircuitOpen: cause unknown\n")
    assert "**No cause could be established from the evidence.**" in refused
    assert "The model named `processor_not_running`, but nothing it cited could be found in what the checks returned" in refused
    assert "### Evidence" not in refused
    assert '| `processor_calls` | "pods are in CrashLoopBackOff" |' in refused


def test_a_replay_and_a_failed_investigation_say_so(tmp_path):
    recorded = investigate(tmp_path / "live")
    replayed = asyncio.run(run.investigate("ProcessorCircuitOpen", ReplayTools(load(recorded.path)),
                                           Named(["processor_errors"], diagnoses=[OUTAGE]), tmp_path / "replays",
                                           replay_of=recorded.id, now=lambda: AT))

    class Away:
        async def ask(self, messages, shape, max_tokens, thinking=False):
            raise ModelUnavailable("The model server does not answer at http://127.0.0.1:8090/v1")

    with pytest.raises(ModelUnavailable):
        asyncio.run(run.investigate("ProcessorCircuitOpen", FakeTools([CIRCUIT_OPEN]), Away(), tmp_path / "failed", now=lambda: AT))

    replay_text = replayed.report_path.read_text(encoding="utf-8")
    assert "(a replay of `20261012T132600Z-ProcessorCircuitOpen`)" in replay_text
    assert "answered from the recording being replayed" in replay_text
    failed = (tmp_path / "failed" / "20261012T132600Z-ProcessorCircuitOpen.md").read_text(encoding="utf-8")
    assert failed.startswith("# ProcessorCircuitOpen: investigation failed\n")
    assert "| Error | ModelUnavailable: The model server does not answer at http://127.0.0.1:8090/v1 |" in failed
    assert "| Steps reached | intake, load_runbook, choose_checks |" in failed
    assert "No diagnosis was made." in failed


def test_a_table_cell_cannot_break_the_table():
    odd = {"check": "processor_errors", "quote": "a | b\nc"}
    text = report._table(("Check", "Quote"), [(odd["check"], odd["quote"])])

    assert text.splitlines()[2] == "| processor_errors | a \\| b c |"


def test_a_runbook_must_have_the_section_the_report_quotes():
    text = ("---\nalert: ProcessorCircuitOpen\ncauses:\n  some_cause: What it means.\nchecks:\n  a:\n    tool: get_saga\n"
            "    tells: x\n---\n# ProcessorCircuitOpen\n\n## What it means\n\nSomething.\n")

    with pytest.raises(runbooks.InvalidRunbook, match='needs a section "## What resolves it"'):
        runbooks.parse(text, "ProcessorCircuitOpen.md")
    with_it = runbooks.parse(text + "\n## What resolves it\n\nIt closes by itself.\nNothing to restart.\n\n## After\n\nMore.\n",
                             "ProcessorCircuitOpen.md")
    assert with_it.resolution == "It closes by itself.\nNothing to restart."


def test_past_investigations_can_be_listed_and_shown_without_anything_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(payflow_agent.config, "RUNS_DIR", tmp_path)
    assert payflow_agent.main(["list"]) == payflow_agent.DONE
    assert "No investigations yet" in capsys.readouterr().out

    first = investigate(tmp_path)
    unknown = investigate(tmp_path, diagnoses=(), at=datetime(2026, 10, 12, 14, 0, 0, tzinfo=UTC))
    replay = asyncio.run(run.investigate("ProcessorCircuitOpen", ReplayTools(load(first.path)), Named(["sagas"], diagnoses=[
        OUTAGE | {"evidence": [{"check": "sagas", "quote": "payments_parked 3"}]}]), tmp_path, replay_of=first.id,
        now=lambda: datetime(2026, 10, 12, 15, 0, 0, tzinfo=UTC)))

    assert payflow_agent.main(["list"]) == payflow_agent.DONE
    assert capsys.readouterr().out.splitlines() == [
        "20261012T150000Z-ProcessorCircuitOpen          processor_outage (high)  [replay]",
        "20261012T140000Z-ProcessorCircuitOpen          cause unknown",
        "20261012T132600Z-ProcessorCircuitOpen          processor_outage (high)",
    ]
    assert payflow_agent.main(["list", "-n", "1"]) == payflow_agent.DONE
    assert len(capsys.readouterr().out.splitlines()) == 1

    for which, expected in (("latest", replay), (first.id, first), (str(unknown.path), unknown), (f"{first.id}.md", first)):
        assert payflow_agent.main(["show", which]) == payflow_agent.DONE
        assert capsys.readouterr().out.rstrip("\n") == report.render(load(expected.path)).rstrip("\n")
    assert payflow_agent.main(["show", "20260101T000000Z-Nothing"]) == payflow_agent.FAILED
    assert "No investigation 20260101T000000Z-Nothing" in capsys.readouterr().out


def test_investigating_prints_the_report_and_where_it_and_the_trace_are(tmp_path, monkeypatch, capsys):
    async def live(alert_name, mcp_url, runs_dir):
        tools = FakeTools([CIRCUIT_OPEN], get_dependency_status=CALLS, get_recent_errors=ERRORS)
        return await run.investigate(alert_name, tools, Named(["processor_errors"], diagnoses=[OUTAGE]), runs_dir, now=lambda: AT)

    monkeypatch.setattr(payflow_agent, "investigate_live", live)
    monkeypatch.setattr(payflow_agent.config, "RUNS_DIR", tmp_path)

    assert payflow_agent.main(["investigate", "ProcessorCircuitOpen"]) == payflow_agent.DONE

    printed = capsys.readouterr().out
    assert printed.startswith("# ProcessorCircuitOpen: processor_outage\n")
    assert f"report  {tmp_path / '20261012T132600Z-ProcessorCircuitOpen.md'}" in printed
    assert f"trace   {tmp_path / '20261012T132600Z-ProcessorCircuitOpen.json'}" in printed
