"""The investigation workflow, as a LangGraph graph.

Version 1: the alert is taken in, its runbook is looked up, evidence is gathered in rounds, a
diagnosis is made and checked against that evidence, and a report is written.

    START -> intake -> load_runbook -> choose_checks <-> gather
                  \\-> END                    \\           /
               (not firing)                 record_unchosen -> diagnose -> report -> END

The model appears in two steps. In choose_checks it names which of the runbook's checks to run
next. In diagnose it names a cause from the runbook's list and quotes the evidence for it.
Everything else is code: which checks and causes exist, how checks are run, when to stop, and
whether each quote is really in the evidence.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agent import diagnosis, prompts, runbooks
from agent.llm import Model
from agent.state import Investigation
from agent.tools import Tools
from agent.trace import Trace

# After this many rounds the investigation moves on with what it has.
MAX_ROUNDS = 4
CHOOSE_MAX_TOKENS = 700
DIAGNOSE_MAX_TOKENS = 1500
# With thinking on, the thinking is counted against the limit as well.
DIAGNOSE_MAX_TOKENS_THINKING = 6000


@dataclass(frozen=True)
class Deps:
    """What the steps work with but do not own: handed to the graph for one run, not kept in its state."""
    tools: Tools
    model: Model
    trace: Trace
    runbooks_dir: Path
    diagnose_thinking: bool = False


class InvestigationError(Exception):
    """The investigation could not be carried out (as opposed to: it found something)."""


async def intake(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Finds the firing alerts with the name asked for. Until the Alertmanager webhook exists
    (phase 3), this reads them from Alertmanager itself; the facts are the same."""
    tools, trace = runtime.context.tools, runtime.context.trace
    async with trace.step("intake") as step:
        listed = await tools.call("list_active_alerts")
        if listed.is_error:
            raise InvestigationError(f"The firing alerts could not be read: {listed.data}")
        firing = listed.data["alerts"]
        alerts = [alert for alert in firing if alert["alert"] == state["alert_name"]]
        if alerts:
            trace.confirm(alerts)
        step["output"] = {"firing_with_this_name": len(alerts), "all_firing": sorted({alert["alert"] for alert in firing})}
        return {"alerts": alerts, "firing_now": step["output"]["all_firing"],
                "status": "investigating" if alerts else "not_firing"}


async def load_runbook(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Looks up the alert's runbook by its exact name. The runbook as it was used is kept in the
    trace, so an old investigation can be read without knowing what the file said back then."""
    trace = runtime.context.trace
    async with trace.step("load_runbook") as step:
        runbook = runbooks.load(state["alert_name"], runtime.context.runbooks_dir)
        trace.document["runbook"] = runbook.as_dict()
        step["output"] = {"runbook": f"{runbook.alert}.md", "checks": [check.name for check in runbook.checks]}
        return {"runbook": runbook.as_dict(), "evidence": {}, "rounds": 0}


async def choose_checks(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """The model names the checks to run next, out of those not run yet, or none when it has
    seen enough. It cannot name anything else (the answer's shape lists exactly the checks that
    remain), and it never writes a tool's arguments: those are the runbook's."""
    async with runtime.context.trace.step("choose_checks") as step:
        remaining = _remaining(state)
        round_number = state["rounds"] + 1
        messages = prompts.choose_checks(state["alert_name"], state["alerts"], state["runbook"], state["evidence"],
                                         remaining, round_number, MAX_ROUNDS)
        reply = await runtime.context.model.ask(messages, prompts.next_checks_shape(remaining, round_number == 1),
                                                CHOOSE_MAX_TOKENS)
        # the schema already limits these; code does not rely on it (a different model server might not enforce it)
        chosen = [name for name in dict.fromkeys(reply.answer.next_checks) if name in remaining][:prompts.MAX_CHECKS_PER_ROUND]
        step["output"] = {"round": round_number, "reasoning": reply.answer.reasoning, "next_checks": chosen}
        if not chosen:
            return {"next_checks": [], "gathering": {"rounds": state["rounds"], "stopped": "the model had seen enough"}}
        return {"next_checks": chosen, "rounds": round_number}


async def gather(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Runs the chosen checks (at once: they do not depend on each other) and adds each result to
    the evidence under its check's name."""
    async with runtime.context.trace.step("gather") as step:
        names = state["next_checks"]
        evidence = state["evidence"] | await _run(names, state, runtime.context.tools)
        left = [name for name in _remaining(state) if name not in names]
        stopped = None
        if not left:
            stopped = "every check was run"
        elif state["rounds"] >= MAX_ROUNDS:
            stopped = f"the limit of {MAX_ROUNDS} rounds was reached"
        step["output"] = {"round": state["rounds"], "checks": names, "findings": {name: _finding(evidence[name]) for name in names}}
        update = {"evidence": evidence, "next_checks": []}
        return update | ({"gathering": {"rounds": state["rounds"], "stopped": stopped}} if stopped else {})


async def record_unchosen(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Runs the checks the model did not ask for, for the record only. They are not evidence and
    nobody reasons over them here. They are in the trace so that this investigation can be
    replayed later with another prompt or another model, which may choose differently."""
    async with runtime.context.trace.step("record_unchosen") as step:
        names = _remaining(state)
        await _run(names, state, runtime.context.tools)
        step["output"] = {"checks": names, "note": "recorded for replays; not part of the evidence"}
        return {"next_checks": []}


async def diagnose(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """The model names the cause and quotes the evidence for it; code checks every quote against
    what the checks really returned. If some are not there, the model is told which and asked
    once more. A cause left with no verified quote is reported as unknown."""
    deps = runtime.context
    async with deps.trace.step("diagnose") as step:
        evidence = state["evidence"]
        if not evidence:
            concluded = {"root_cause": runbooks.UNKNOWN, "confidence": "low", "evidence": [], "unverified": [], "attempts": 0,
                         "set_aside": [],
                         "reasoning": "", "summary": "No cause is reported: no check was run, so there is no evidence."}
            step["output"] = concluded
            return {"diagnosis": concluded}

        shape = prompts.diagnosis_shape([cause["name"] for cause in state["runbook"]["causes"]], list(evidence))
        max_tokens = DIAGNOSE_MAX_TOKENS_THINKING if deps.diagnose_thinking else DIAGNOSE_MAX_TOKENS
        messages = prompts.diagnose(state["alert_name"], state["alerts"], state["runbook"], evidence)
        answer = (await deps.model.ask(messages, shape, max_tokens, deps.diagnose_thinking)).answer.model_dump()
        verified, unverified = diagnosis.verify(answer["evidence"], evidence)
        attempts = 1
        if unverified:
            again = prompts.retry_diagnosis(messages, answer, unverified)
            answer = (await deps.model.ask(again, shape, max_tokens, deps.diagnose_thinking)).answer.model_dump()
            verified, unverified = diagnosis.verify(answer["evidence"], evidence)
            attempts = 2
        concluded = diagnosis.conclude(answer, verified, unverified, attempts)
        concluded["set_aside"] = diagnosis.set_aside(answer, evidence)
        step["output"] = concluded
        return {"diagnosis": concluded}


async def report(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """What the investigation gathered and concluded."""
    async with runtime.context.trace.step("report") as step:
        alerts = state["alerts"]
        evidence = state["evidence"]
        result = {
            "alert": state["alert_name"],
            "severity": alerts[0]["severity"],
            "firing_for_minutes": max(alert["firing_for_minutes"] for alert in alerts),
            "affected": [alert["about"] for alert in alerts],
            "checks_available": [check["name"] for check in state["runbook"]["checks"]],
            "checks_run": list(evidence),
            "rounds": state["gathering"]["rounds"],
            "stopped_because": state["gathering"]["stopped"],
            "findings": {name: _finding(item) for name, item in evidence.items()},
            "diagnosis": state["diagnosis"],
            "summary": state["diagnosis"]["summary"],
        }
        step["output"] = result
        return {"result": result, "status": "completed"}


def _remaining(state: Investigation) -> list[str]:
    return [check["name"] for check in state["runbook"]["checks"] if check["name"] not in state["evidence"]]


async def _run(names: list[str], state: Investigation, tools: Tools) -> dict[str, dict]:
    checks = {check["name"]: check for check in state["runbook"]["checks"]}
    results = await asyncio.gather(*(tools.call(checks[name]["tool"], checks[name]["arguments"]) for name in names))
    return {name: {"tool": checks[name]["tool"], "arguments": checks[name]["arguments"],
                   "is_error": result.is_error, "result": result.data}
            for name, result in zip(names, results, strict=True)}


def _finding(item: dict) -> list[str]:
    """What a check found, in the tool's own words: its concerns, or why it could not be run."""
    if item["is_error"]:
        return [f"could not be run: {item['result']}"]
    return list(item["result"].get("concerns", [])) if isinstance(item["result"], dict) else []


def build_graph():
    graph = StateGraph(Investigation, context_schema=Deps)
    for step in (intake, load_runbook, choose_checks, gather, record_unchosen, diagnose, report):
        graph.add_node(step.__name__, step)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", lambda state: "load_runbook" if state["status"] == "investigating" else END,
                                ["load_runbook", END])
    graph.add_edge("load_runbook", "choose_checks")
    graph.add_conditional_edges("choose_checks", lambda state: "gather" if state["next_checks"] else "record_unchosen",
                                ["gather", "record_unchosen"])
    graph.add_conditional_edges("gather", lambda state: "record_unchosen" if state.get("gathering") else "choose_checks",
                                ["record_unchosen", "choose_checks"])
    graph.add_edge("record_unchosen", "diagnose")
    graph.add_edge("diagnose", "report")
    graph.add_edge("report", END)
    return graph.compile()
