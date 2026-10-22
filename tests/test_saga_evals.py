"""The SagaRequiresAttention scenarios (evals/scenarios/sagas): their recordings, and the one rule
graded on code rather than the model, the retry list."""
import asyncio
from datetime import UTC, datetime

from agent import run
from agent.tools import ReplayTools
from agent.trace import load
from cli import payflow_agent
from evals import derive, derive_sagas, grade, scenario
from tests.fakes import ScriptedModel

AT = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def replayed(item: scenario.Scenario, tmp_path, model=None) -> dict:
    trace = asyncio.run(run.investigate(item.alert, ReplayTools(load(item.recording)), model or ScriptedModel(), tmp_path,
                                        now=lambda: AT))
    return load(trace.path)


def test_the_saga_set_has_each_cause_live_or_derived_and_three_planted_instructions():
    saga_set = {item.name: item for item in scenario.load_all(scenario.SAGAS_DIR)}

    assert len(saga_set) == 9 and all(name.startswith("sagas-") for name in saga_set)
    assert {item.root_causes[0] for item in saga_set.values()} == {"processor_no_answer", "bank_no_answer",
                                                                    "partner_rejected", "ledger_rejected"}
    assert sum(item.has_planted_instruction for item in saga_set.values()) == 3
    assert all(item.retry is not None for item in saga_set.values())
    assert not set(saga_set) & {item.name for item in scenario.load_all()}
    assert {d.recording for d in derive_sagas.SAGAS} | {f"SagaRequiresAttention-captures-parked-{n}.json" for n in (1, 2, 3)} == {
        item.recording.name for item in saga_set.values()}


def test_whatever_the_model_answers_code_proposes_exactly_the_sagas_each_scenario_expects(tmp_path):
    """The scripted model chooses nothing and diagnoses nothing: the list is code's alone."""
    for item in scenario.load_all(scenario.SAGAS_DIR):
        document = replayed(item, tmp_path / item.name)
        retried = sorted(row["id"] for row in (document["result"]["proposal"] or {}).get("retry", []))
        assert retried == sorted(item.retry), item.name


def test_a_planted_instruction_is_in_the_last_error_whole_and_nowhere_else():
    for demand, text in derive_sagas.INSTRUCTIONS.items():
        derived = derive.derive(next(d for d in derive_sagas.SAGAS if d.recording.endswith(f"planted-{demand}.json")))
        errors = [saga["last_error"] for step in derived["steps"] for call in step["tool_calls"]
                  if call["tool"] in ("list_sagas_needing_attention", "list_parked_sagas") for saga in call["result"]["sagas"]]
        assert sum(error.endswith(text) for error in errors) == 2      # the one saga, in both saga tools
        assert all(len(error) <= 200 for error in errors)


def test_the_bank_scenario_is_about_payout_service_so_code_reads_payout_sagas(tmp_path):
    item = next(item for item in scenario.load_all(scenario.SAGAS_DIR) if item.name == "sagas-bank-no-answer")

    document = replayed(item, tmp_path)

    assert document["alerts"][0]["about"] == {"application": "payout-service"}
    calls = [call for step in document["steps"] for call in step["tool_calls"] if call["tool"] == "list_parked_sagas"]
    assert [call["arguments"] for call in calls] == [{"service": "payout-service"}]


def test_a_wrong_retry_list_fails_a_run_whose_diagnosis_is_right():
    item = next(item for item in scenario.load_all(scenario.SAGAS_DIR) if item.name == "sagas-ledger-rejected")
    document = {"status": "completed", "id": "t", "steps": [], "result": {
        "diagnosis": {"root_cause": "ledger_rejected", "confidence": "high", "summary": "", "evidence": [
            {"check": "parked_sagas", "quote": "x"}], "unverified": [], "attempts": 1},
        "checks_run": ["parked_sagas"], "rounds": 1, "stopped_because": "x",
        "proposal": {"cause": "ledger_rejected", "step": "x", "retry": [{"id": "pay_somebody-else"}]}}}

    graded = grade.grade(item, document)

    assert graded["cause_right"] and graded["retry_right"] is False and not graded["passed"]
    document["result"]["proposal"]["retry"] = [{"id": item.retry[0]}]
    assert grade.grade(item, document)["passed"]


def test_the_eval_command_runs_the_saga_scenarios_on_their_own(monkeypatch, capsys):
    ran = {}

    async def fake_run(scenarios, model, label):
        ran["names"] = [item.name for item in scenarios]
        return {"id": "x", "totals": {"passed": 0, "scenarios": len(scenarios)}}

    monkeypatch.setattr(payflow_agent.run_eval, "run_eval", fake_run)
    monkeypatch.setattr(payflow_agent.run_eval, "render", lambda results: "")
    monkeypatch.setattr(payflow_agent.runs, "local_model", lambda: None)

    payflow_agent.main(["eval", "--sagas"])

    assert len(ran["names"]) == 9 and all(name.startswith("sagas-") for name in ran["names"])
