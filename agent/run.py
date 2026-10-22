"""Runs one investigation, live or replayed, and returns its trace."""
from pathlib import Path

from langgraph.types import Command
from mcp import Client

from agent import config, report, runbooks
from agent import trace as traces
from agent.graph import Deps, build_graph
from agent.llm import LocalModel, Model, RecordingModel
from agent.operator import Operator
from agent.tools import LiveTools, RecordingTools, ReplayTools, Tools
from agent.trace import Trace


class NotFiring(Exception):
    """The alert asked for is not firing: there is nothing to investigate."""

    def __init__(self, alert_name: str, firing_now: list[str]):
        self.alert_name = alert_name
        self.firing_now = firing_now
        firing = ", ".join(firing_now) if firing_now else "nothing"
        super().__init__(f"{alert_name} is not firing. Firing now: {firing}.")


# The status of a trace whose workflow is paused at a proposal, waiting for a person's decision.
AWAITING_DECISION = "awaiting_decision"


async def investigate(alert_name: str, tools: Tools, model: Model, runs_dir: Path, replay_of: str | None = None,
                      now=traces.utc_now, runbooks_dir: Path | None = None, checkpointer=None,
                      ask_for_decision: bool = False) -> Trace:
    """One run of the workflow. The trace file is written from the moment the alert is confirmed
    to be firing, and rewritten after every step. With ask_for_decision (the service), a run that
    reaches a proposal pauses there: its trace is left AWAITING_DECISION, its state is in the
    checkpointer under the trace's id, and resume() finishes it."""
    if ask_for_decision and checkpointer is None:
        raise ValueError("a workflow that waits for a decision needs a checkpointer to keep its state")
    runbooks_dir = runbooks_dir or config.RUNBOOKS_DIR
    # An alert without a runbook is refused before anything is asked or written (NoRunbook).
    runbooks.load(alert_name, runbooks_dir)
    trace = Trace(alert_name, runs_dir, replay_of, now)
    trace.document["model"] = getattr(model, "name", type(model).__name__)
    deps = Deps(tools=RecordingTools(tools, trace), model=RecordingModel(model, trace), trace=trace,
                runbooks_dir=runbooks_dir, diagnose_thinking=config.DIAGNOSE_THINKING, ask_for_decision=ask_for_decision)
    try:
        final = await build_graph(checkpointer).ainvoke({"alert_name": alert_name}, config=_thread(trace.id, checkpointer),
                                                        context=deps)
    except Exception as e:
        trace.finish("failed", error=f"{type(e).__name__}: {e}")
        trace.write_report(report.render(trace.document))
        raise
    if final["status"] == "not_firing":
        raise NotFiring(alert_name, final["firing_now"])
    trace.finish(AWAITING_DECISION if "__interrupt__" in final else "completed", result=final["result"])
    trace.write_report(report.render(trace.document))
    return trace


async def resume(trace_path: Path, decision: dict, checkpointer, runbooks_dir: Path | None = None, now=traces.utc_now,
                 tools: Tools | None = None, operator: Operator | None = None, **timing) -> Trace:
    """Finishes an investigation paused at its proposal, with a person's decision
    ({"decision": approved | rejected | lapsed, "by", "at", "note"}). It may run in another process
    than the one that paused it: the workflow's state comes from the checkpointer, its trace from
    the file. The model is never asked. An approved retry list is carried out with the tools (the
    partners' state, recorded in the trace) and the operator login; nothing else uses them.
    Called again after a restart that cut the carrying out short, it goes on from the last saga
    done: the decision is already in the saved state and is not taken again.
    timing: sleep and clock, for tests of the watch."""
    trace = Trace.reopen(trace_path, now)
    if trace.document["status"] != AWAITING_DECISION:
        raise ValueError(f"investigation {trace.id} is {trace.document['status']}, not waiting for a decision")
    deps = Deps(tools=RecordingTools(tools, trace) if tools is not None else _Unused("tools"), model=_Unused("model"),
                trace=trace, runbooks_dir=runbooks_dir or config.RUNBOOKS_DIR, ask_for_decision=True, operator=operator,
                **timing)
    # After a restart past the decision, LangGraph goes on from the last saved step: the decision
    # already answered the pause, and this one is not used (tests/test_carry_out.py).
    final = await build_graph(checkpointer).ainvoke(Command(resume=decision), config=_thread(trace.id, checkpointer),
                                                    context=deps)
    trace.finish("completed", result=final["result"])
    trace.write_report(report.render(trace.document))
    return trace


def _thread(trace_id: str, checkpointer) -> dict | None:
    """The workflow's saved state is kept under the investigation's id."""
    return {"configurable": {"thread_id": trace_id}} if checkpointer is not None else None


class _Unused:
    """In place of the tools and the model after a decision: nothing may call them then."""

    def __init__(self, what: str):
        self._what = what

    async def call(self, *args, **kwargs):
        raise RuntimeError(f"the {self._what} were called after the decision")

    async def ask(self, *args, **kwargs):
        raise RuntimeError(f"the {self._what} was asked after the decision")


async def resume_live(trace_path: Path, decision: dict, mcp_url: str, checkpointer, operator: Operator) -> Trace:
    """resume() in the service. Only an approval can carry anything out, so only an approval
    connects to the MCP server."""
    if decision["decision"] != "approved":
        return await resume(trace_path, decision, checkpointer)
    failure: Exception | None = None
    trace: Trace | None = None
    async with Client(mcp_url) as client:
        try:
            trace = await resume(trace_path, decision, checkpointer, tools=LiveTools(client), operator=operator)
        except Exception as e:  # noqa: BLE001 - raised again below, outside the block (see investigate_live)
            failure = e
    if failure is not None:
        raise failure
    return trace


def operator_login() -> Operator:
    """The agent's operator login to PayFlow, from the settings (config.PAYFLOW_OPERATOR_*)."""
    return Operator(config.PAYFLOW_URL, config.PAYFLOW_OPERATOR_CLIENT_ID, config.PAYFLOW_OPERATOR_CLIENT_SECRET)


async def investigate_live(alert_name: str, mcp_url: str, runs_dir: Path, checkpointer=None,
                           ask_for_decision: bool = False) -> Trace:
    """Against the live system, through the MCP server."""
    # The MCP client runs its connection in a task group, and an exception that leaves the
    # "async with" comes out wrapped in an ExceptionGroup. So the workflow's own outcome (not
    # firing, a failed step) is carried out of the block by hand and raised after it.
    failure: Exception | None = None
    trace: Trace | None = None
    async with Client(mcp_url) as client:
        try:
            trace = await investigate(alert_name, LiveTools(client), local_model(), runs_dir, checkpointer=checkpointer,
                                      ask_for_decision=ask_for_decision)
        except Exception as e:  # noqa: BLE001 - not swallowed: raised again below, outside the block
            failure = e
    if failure is not None:
        raise failure
    return trace


async def investigate_replay(recorded_path: Path, runs_dir: Path) -> Trace:
    """The same alert again, with the tool results an earlier investigation recorded: no MCP
    server, no cluster. The model is asked afresh, so a changed prompt, runbook or model is tried
    against the same evidence. Writes a trace of its own, marked as a replay."""
    recorded = traces.load(recorded_path)
    return await investigate(recorded["alert_name"], ReplayTools(recorded), local_model(), runs_dir,
                             replay_of=recorded["id"])


def refused_connection(error: BaseException) -> bool:
    """Whether a failure is a server that refused the connection (the MCP server not running).
    The MCP client reports it wrapped in exception groups, and as the ConnectError of whichever
    HTTP library it is built on (httpx2 today), hence by name."""
    if type(error).__name__ == "ConnectError":
        return True
    nested = list(getattr(error, "exceptions", ())) + [error.__cause__, error.__context__]
    return any(refused_connection(inner) for inner in nested if inner is not None)


def local_model() -> LocalModel:
    return LocalModel(config.LLM_URL, config.LLM_MODEL, cache_prompt=config.LLM_CACHE_PROMPT)
