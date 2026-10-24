"""The investigation workflow, as a LangGraph graph.

Version 1: the alert is taken in, its runbook is looked up, evidence is gathered in rounds, a
diagnosis is made and checked against that evidence, and a report is written.

    START -> intake -> load_runbook -> choose_checks <-> gather
                  \\-> END                    \\           /
               (not firing)                 record_unchosen -> sort_sagas -> sort_days -> diagnose
                                                -> screen -> propose -> report -> decide -> END
                                                         \\-> retry <-> retry -> watch -> END
                                                       (an approved retry list)

After an approved retry list, everything is code: each saga is checked again and retried, one
per step (so the saved state after a restart says exactly which are done), and then watched.

The model appears in two steps. In choose_checks it names which of the runbook's checks to run
next. In diagnose it names a cause from the runbook's list and quotes the evidence for it.
Everything else is code: which checks and causes exist, how checks are run, when to stop, and
whether each quote is really in the evidence.
"""
import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agent import carry_out, diagnosis, prompts, reruns, runbooks, sagas
from agent.llm import Model, ModelError
from agent.operator import Operator, OperatorUnavailable, Refused
from agent.state import Investigation
from agent.tools import Tools
from agent.trace import Trace

# After this many rounds the investigation moves on with what it has.
MAX_ROUNDS = 4
# gpt-oss reasons before it answers, and that counts too: 700, then 1000 ran out with the eight
# checks of ReconciliationRunFailed (one answer took 1133). An answer that fits is the same under
# any higher limit.
CHOOSE_MAX_TOKENS = 2000
DIAGNOSE_MAX_TOKENS = 1500
SCREEN_MAX_TOKENS = 1000  # 500 cut off one gpt-oss answer: it reasons a little before answering
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
    # True in the service: the workflow pauses at a proposal until a person decides (it then needs
    # a checkpointer). False on the command line and in evals: the proposal is reported, not asked.
    ask_for_decision: bool = False
    # The operator login, to carry out an approved retry list (only the service's resume has one).
    operator: Operator | None = None
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)


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
        runbook = runbooks.resolve(runbooks.load(state["alert_name"], runtime.context.runbooks_dir).as_dict(), state["alerts"])
        trace.document["runbook"] = runbook
        step["output"] = {"runbook": f"{runbook['alert']}.md", "checks": [check["name"] for check in runbook["checks"]]}
        if runbook["unresolved_checks"]:
            step["output"]["unresolved_checks"] = runbook["unresolved_checks"]
        return {"runbook": runbook, "evidence": {}, "rounds": 0}


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
    what the checks really returned. If some are not there, or the runbook's rules for the cause do
    not hold (diagnosis.unsupported), the model is told why and asked once more. A cause that still
    does not stand is reported as unknown."""
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
        by_code = _sorted_by_code(state)
        unsupported = diagnosis.unsupported(answer, verified, evidence, state["runbook"], by_code)
        attempts = 1
        if unverified or unsupported:
            again = prompts.retry_diagnosis(messages, answer, unverified, unsupported)
            answer = (await deps.model.ask(again, shape, max_tokens, deps.diagnose_thinking)).answer.model_dump()
            verified, unverified = diagnosis.verify(answer["evidence"], evidence)
            unsupported = diagnosis.unsupported(answer, verified, evidence, state["runbook"], by_code)
            attempts = 2
        concluded = diagnosis.conclude(answer, verified, unverified, attempts, unsupported)
        concluded["set_aside"] = diagnosis.set_aside(answer, evidence)
        step["output"] = concluded
        return {"diagnosis": concluded}


async def screen(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """One narrow question about only the text outside parties wrote (log lines, error causes, a
    saga's last error): does any of it speak to a reader, an agent or an operator? What the model
    quotes, and code finds there, joins what the diagnosis set aside, and the report shows it.
    The diagnosis is not touched. Nothing to screen, nothing asked; a garbled answer is noted and
    the investigation goes on (the answer is a warning for the operator, not the cause)."""
    deps = runtime.context
    async with deps.trace.step("screen") as step:
        texts = diagnosis.outside_texts(state["evidence"])
        if not texts:
            step["output"] = {"texts": 0, "found": []}
            return {}
        shape = prompts.screening_shape(sorted({check for check, _ in texts}))
        try:
            reply = await deps.model.ask(prompts.screen_outside_text(texts), shape, SCREEN_MAX_TOKENS)
        except ModelError as e:
            step["output"] = {"texts": len(texts), "found": [], "error": str(e)}
            return {}
        found = diagnosis.set_aside({"set_aside": reply.answer.model_dump().get("addressed_to_a_reader") or []},
                                    state["evidence"])
        step["output"] = {"texts": len(texts), "found": found}
        set_aside = diagnosis.merge_set_aside(state["diagnosis"].get("set_aside", []), found)
        return {"diagnosis": state["diagnosis"] | {"set_aside": set_aside}}


async def sort_sagas(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Only for a runbook about parked sagas (it has parking codes): reads every parked saga of the
    services the alert is about and sorts them, by the code each parked for, into those the agent
    may propose to retry and those that need a person (agent/sagas.py). Code decides this, not the
    model: the diagnosis explains the incident; it cannot add a saga to the list or drop one."""
    if not state["runbook"].get("parking_codes"):
        return {}
    async with runtime.context.trace.step("sort_sagas") as step:
        parked = await sagas.read(sagas.services_of(state["alerts"]), runtime.context.tools)
        sorted_ = sagas.sort(parked, state["runbook"])
        step["output"] = sorted_
        return {"sagas": sorted_}


async def sort_days(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Only for a runbook with error sources (ReconciliationRunFailed): reads every day of the last
    week that no run has reconciled and whose newest run failed, and sorts them, by the source each
    one's error names, into those the agent may propose to run again and those that need a person
    (agent/reruns.py). Code decides this, not the model."""
    if not state["runbook"].get("error_sources"):
        return {}
    async with runtime.context.trace.step("sort_days") as step:
        sorted_ = reruns.sort(await reruns.read(runtime.context.tools), state["runbook"])
        step["output"] = sorted_
        return {"days": sorted_}


async def propose(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """The runbook's next step for the cause found. None for unknown, or a cause without one.
    For parked sagas, the retry list code made goes with it, whatever the cause: a retry is
    proposed exactly when that list has sagas on it, and a retryable cause with an empty list
    proposes nothing (there is nothing to retry)."""
    async with runtime.context.trace.step("propose") as step:
        cause = state["diagnosis"]["root_cause"]
        text = next((item.get("proposal", "") for item in state["runbook"]["causes"] if item["name"] == cause), "")
        sorted_ = state.get("sagas")
        days = state.get("days")
        if days is not None:
            proposal = _with_list(cause, text, days["rerun"], "rerun", state["runbook"]["rerunnable"], reruns.RERUN_STEP)
        elif sorted_ is None:
            proposal = {"cause": cause, "step": text} if text else None
        else:
            proposal = _with_list(cause, text, sorted_["retry"], "retry", state["runbook"]["retryable"], sagas.RETRY_STEP)
        step["output"] = proposal
        return {"proposal": proposal}


async def decide(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Waits for a person to approve or reject the proposal (only in the service, and only when
    there is one). The workflow pauses here: its state is saved, and a decision (POST
    /investigations/{id}/decision) resumes it, even in another process. An approved retry list is
    carried out next (retry, watch); any other approval only records the decision."""
    if not (state.get("proposal") and runtime.context.ask_for_decision):
        return {}
    # Outside the trace's step: the pause leaves the step, and the step is run again on resume.
    answer = interrupt({"proposal": state["proposal"], "investigation": runtime.context.trace.id})
    async with runtime.context.trace.step("decide") as step:
        decision = {key: answer.get(key) for key in ("decision", "by", "at", "note")}
        step["output"] = decision
        return {"decision": decision, "result": state["result"] | {"decision": decision}}


async def retry(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """The next saga of the approved list: checked again, and retried if it still qualifies
    (agent/carry_out.py). One saga per step: the state saved after each says which are done, so a
    restart goes on with the next and never retries one twice."""
    deps = runtime.context
    if deps.operator is None:
        raise InvestigationError("an approved retry list needs the operator login, and none was given")
    async with deps.trace.step("retry") as step:
        done = state.get("carried_out", [])
        row = _next_to_retry(state)
        outcome = await carry_out.retry_one(row, deps.operator, deps.tools)
        step["output"] = outcome
        return {"carried_out": [*done, outcome]}


async def watch(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Watches each retried saga until it finishes or parks again, for up to WATCH_SECONDS, and
    records how each ended (or that it was still running)."""
    deps = runtime.context
    async with deps.trace.step("watch") as step:
        carried = [dict(item) for item in state.get("carried_out", [])]
        waiting = {index: item for index, item in enumerate(carried) if item["outcome"] == carry_out.RETRIED}
        deadline = deps.clock() + carry_out.WATCH_SECONDS
        last_seen: dict[int, str] = {}
        while waiting:
            for index, item in list(waiting.items()):
                try:
                    now = await deps.operator.saga(item["id"], item["saga"])
                except (Refused, OperatorUnavailable) as e:
                    last_seen[index] = f"could not be read: {e}"
                    continue
                ended = carry_out.settled(now)
                if ended:
                    item["ended"] = ended
                    del waiting[index]
                else:
                    last_seen[index] = f"still running, in {now['state']}"
            if not waiting or deps.clock() >= deadline:
                break
            await deps.sleep(carry_out.WATCH_EVERY_SECONDS)
        for index, item in waiting.items():
            item["ended"] = f"{last_seen.get(index, 'not seen')} after {carry_out.WATCH_SECONDS // 60} minutes of watching"
        step["output"] = {"watched_seconds": carry_out.WATCH_SECONDS, "sagas": carried}
        return {"carried_out": carried, "result": state["result"] | {"carried_out": carried}}


async def rerun(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """The next day of the approved rerun list: checked again, and its reconciliation run again if it
    still qualifies (agent/carry_out.py). One day per step, like the saga retries."""
    deps = runtime.context
    if deps.operator is None:
        raise InvestigationError("an approved rerun list needs the operator login, and none was given")
    async with deps.trace.step("rerun") as step:
        done = state.get("carried_out", [])
        outcome = await carry_out.rerun_one(_next_to_rerun(state), deps.operator, deps.tools)
        step["output"] = outcome
        return {"carried_out": [*done, outcome]}


async def watch_runs(state: Investigation, runtime: Runtime[Deps]) -> dict:
    """Watches each rerun still going after PayFlow's answer, for up to RUN_WATCH_SECONDS, and
    records how each ended (or that it was still going)."""
    deps = runtime.context
    async with deps.trace.step("watch_runs") as step:
        carried = [dict(item) for item in state.get("carried_out", [])]
        for item in carried:
            if item["outcome"] == carry_out.RERUN:
                item["ended"] = carry_out.run_settled(item["run"])
        waiting = [item for item in carried if item["outcome"] == carry_out.RERUN and item["ended"] is None]
        deadline = deps.clock() + carry_out.RUN_WATCH_SECONDS
        while waiting and deps.clock() < deadline:
            await deps.sleep(carry_out.RUN_WATCH_EVERY_SECONDS)
            for item in list(waiting):
                try:
                    runs = await deps.operator.runs(item["id"])
                except (Refused, OperatorUnavailable):
                    continue
                ended = carry_out.run_settled(runs[0] if runs else None)
                if ended:
                    item["ended"] = ended
                    waiting.remove(item)
        for item in waiting:
            item["ended"] = f"still running after {carry_out.RUN_WATCH_SECONDS // 60} minutes of watching"
        step["output"] = {"watched_seconds": carry_out.RUN_WATCH_SECONDS, "days": carried}
        return {"carried_out": carried, "result": state["result"] | {"carried_out": carried}}


def _sorted_by_code(state: Investigation) -> list[dict]:
    """Every saga or day code has sorted into a cause (sort_sagas, sort_days), whatever list it is on."""
    rows = []
    for key, lists in (("sagas", ("retry", "needs_a_person")), ("days", ("rerun", "needs_a_person"))):
        rows += [row for name in lists for row in (state.get(key) or {}).get(name, [])]
    return rows


def _with_list(cause: str, text: str, listed: list[dict], key: str, acting_causes, default_step: str) -> dict | None:
    """A proposal with the list code made (sagas to retry, days to rerun), whatever the cause: an
    action is proposed exactly when the list has something on it, and an acting cause with an empty
    list proposes nothing (there is nothing to do)."""
    if listed:
        return {"cause": cause, "step": text if cause in acting_causes else default_step, key: listed}
    return {"cause": cause, "step": text} if text and cause not in acting_causes else None


def _next_to_rerun(state: Investigation) -> dict | None:
    done = {item["id"] for item in state.get("carried_out", [])}
    return next((row for row in state["proposal"]["rerun"] if row["id"] not in done), None)


def _after_rerun(state: Investigation) -> str:
    return "rerun" if _next_to_rerun(state) else "watch_runs"


def _next_to_retry(state: Investigation) -> dict | None:
    done = {item["id"] for item in state.get("carried_out", [])}
    return next((row for row in state["proposal"]["retry"] if row["id"] not in done), None)


def _after_decide(state: Investigation) -> str:
    approved = (state.get("decision") or {}).get("decision") == "approved"
    proposal = state.get("proposal") or {}
    if approved and proposal.get("retry"):
        return "retry"
    return "rerun" if approved and proposal.get("rerun") else END


def _after_retry(state: Investigation) -> str:
    return "retry" if _next_to_retry(state) else "watch"


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
            "proposal": state.get("proposal"),
            "decision": None,
        }
        if state.get("sagas") is not None:
            result["sagas"] = state["sagas"]
        if state.get("days") is not None:
            result["days"] = state["days"]
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


def build_graph(checkpointer=None):
    """checkpointer: where the state is saved after every step, so that a paused investigation can
    be resumed (the service's); None where nothing pauses."""
    graph = StateGraph(Investigation, context_schema=Deps)
    for step in (intake, load_runbook, choose_checks, gather, record_unchosen, diagnose, screen, sort_sagas, sort_days,
                 propose, report, decide, retry, watch, rerun, watch_runs):
        graph.add_node(step.__name__, step)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", lambda state: "load_runbook" if state["status"] == "investigating" else END,
                                ["load_runbook", END])
    graph.add_edge("load_runbook", "choose_checks")
    graph.add_conditional_edges("choose_checks", lambda state: "gather" if state["next_checks"] else "record_unchosen",
                                ["gather", "record_unchosen"])
    graph.add_conditional_edges("gather", lambda state: "record_unchosen" if state.get("gathering") else "choose_checks",
                                ["record_unchosen", "choose_checks"])
    # code's sorting comes before the diagnosis, which it checks (diagnosis.unsupported)
    graph.add_edge("record_unchosen", "sort_sagas")
    graph.add_edge("sort_sagas", "sort_days")
    graph.add_edge("sort_days", "diagnose")
    graph.add_edge("diagnose", "screen")
    graph.add_edge("screen", "propose")
    graph.add_edge("propose", "report")
    graph.add_edge("report", "decide")
    graph.add_conditional_edges("decide", _after_decide, ["retry", "rerun", END])
    graph.add_conditional_edges("retry", _after_retry, ["retry", "watch"])
    graph.add_edge("watch", END)
    graph.add_conditional_edges("rerun", _after_rerun, ["rerun", "watch_runs"])
    graph.add_edge("watch_runs", END)
    return graph.compile(checkpointer=checkpointer)
