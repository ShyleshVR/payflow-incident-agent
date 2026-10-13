"""Runs one investigation, live or replayed, and returns its trace."""
from pathlib import Path

from mcp import Client

from agent import config, report, runbooks
from agent import trace as traces
from agent.graph import Deps, build_graph
from agent.llm import LocalModel, Model, RecordingModel
from agent.tools import LiveTools, RecordingTools, ReplayTools, Tools
from agent.trace import Trace


class NotFiring(Exception):
    """The alert asked for is not firing: there is nothing to investigate."""

    def __init__(self, alert_name: str, firing_now: list[str]):
        self.alert_name = alert_name
        self.firing_now = firing_now
        firing = ", ".join(firing_now) if firing_now else "nothing"
        super().__init__(f"{alert_name} is not firing. Firing now: {firing}.")


async def investigate(alert_name: str, tools: Tools, model: Model, runs_dir: Path, replay_of: str | None = None,
                      now=traces.utc_now, runbooks_dir: Path | None = None) -> Trace:
    """One run of the workflow. The trace file is written from the moment the alert is confirmed
    to be firing, and rewritten after every step."""
    runbooks_dir = runbooks_dir or config.RUNBOOKS_DIR
    # An alert without a runbook is refused before anything is asked or written (NoRunbook).
    runbooks.load(alert_name, runbooks_dir)
    trace = Trace(alert_name, runs_dir, replay_of, now)
    trace.document["model"] = getattr(model, "name", type(model).__name__)
    deps = Deps(tools=RecordingTools(tools, trace), model=RecordingModel(model, trace), trace=trace,
                runbooks_dir=runbooks_dir, diagnose_thinking=config.DIAGNOSE_THINKING)
    try:
        final = await build_graph().ainvoke({"alert_name": alert_name}, context=deps)
    except Exception as e:
        trace.finish("failed", error=f"{type(e).__name__}: {e}")
        trace.write_report(report.render(trace.document))
        raise
    if final["status"] == "not_firing":
        raise NotFiring(alert_name, final["firing_now"])
    trace.finish("completed", result=final["result"])
    trace.write_report(report.render(trace.document))
    return trace


async def investigate_live(alert_name: str, mcp_url: str, runs_dir: Path) -> Trace:
    """Against the live system, through the MCP server."""
    # The MCP client runs its connection in a task group, and an exception that leaves the
    # "async with" comes out wrapped in an ExceptionGroup. So the workflow's own outcome (not
    # firing, a failed step) is carried out of the block by hand and raised after it.
    failure: Exception | None = None
    trace: Trace | None = None
    async with Client(mcp_url) as client:
        try:
            trace = await investigate(alert_name, LiveTools(client), local_model(), runs_dir)
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


def local_model() -> LocalModel:
    return LocalModel(config.LLM_URL, config.LLM_MODEL, cache_prompt=config.LLM_CACHE_PROMPT)
