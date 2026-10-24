"""The ReconciliationRunFailed scenarios (evals/scenarios/reconciliation): their recordings, and the
rule graded on code rather than the model, the rerun list."""
import asyncio
from datetime import UTC, datetime

from agent import run
from agent.tools import ReplayTools
from agent.trace import load
from evals import derive, derive_reconciliation, scenario
from mcp_server.tools import logs
from tests.fakes import ScriptedModel

AT = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def test_the_set_has_every_cause_four_of_them_live_and_three_planted_instructions():
    found = {item.name: item for item in scenario.load_all(scenario.RECONCILIATION_DIR)}

    assert len(found) == 10 and all(name.startswith("reconciliation-") for name in found)
    assert {item.root_causes[0] for item in found.values()} == {
        "processor_unavailable", "ledger_unavailable", "payments_unavailable", "payouts_unavailable", "auth_unavailable",
        "run_abandoned"}
    assert sum(item.has_planted_instruction for item in found.values()) == 3
    derived = {d.recording for d in derive_reconciliation.RECONCILIATION}
    assert len({item.recording.name for item in found.values()} - derived) == 4      # the live ones


def test_whatever_the_model_answers_code_proposes_exactly_the_days_each_scenario_expects(tmp_path):
    for item in scenario.load_all(scenario.RECONCILIATION_DIR):
        trace = asyncio.run(run.investigate(item.alert, ReplayTools(load(item.recording)), ScriptedModel(), tmp_path / item.name,
                                            now=lambda: AT))
        listed = sorted(row["id"] for row in (load(trace.path)["result"]["proposal"] or {}).get("rerun", []))
        assert listed == sorted(item.retry), item.name


def test_a_planted_instruction_arrives_whole_in_the_log_and_the_run_error():
    for demand, text in derive_reconciliation.INSTRUCTIONS.items():
        derived = derive.derive(next(d for d in derive_reconciliation.RECONCILIATION if d.recording.endswith(f"planted-{demand}.json")))
        lines = [group["message"] for step in derived["steps"] for call in step["tool_calls"]
                 if call["tool"] == "get_recent_errors" and call["arguments"]["workload"] == "reconciliation-service"
                 for group in call["result"]["groups"]]
        assert lines and all(text in line and len(line) <= logs.MAX_MESSAGE for line in lines)
