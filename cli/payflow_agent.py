"""The agent's command line.

    payflow-agent investigate ProcessorCircuitOpen       # the alert must be firing now
    payflow-agent investigate --replay runs\\<id>.json    # again, from a recorded investigation
    payflow-agent list                                   # recent investigations
    payflow-agent show latest                            # the report of one (latest, an id, or a file)
    payflow-agent eval --label baseline                  # replay every recorded scenario and score it

A live investigation needs the model server (scripts\\start-llm.ps1), the MCP server
(python -m mcp_server.server) and the PayFlow cluster. A replay or an eval needs only the model server.
Listing and showing need nothing: reports are made from the trace files in runs\\.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from agent import config, report
from agent import run as runs
from agent import trace as traces
from agent.graph import InvestigationError
from agent.llm import ModelError, ModelUnavailable
from agent.run import NotFiring, investigate_live, investigate_replay
from agent.runbooks import InvalidRunbook, NoRunbook
from agent.tools import ReplayMismatch
from evals import run_eval, scenario

DONE, NOT_FIRING, FAILED, NO_RUNBOOK = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="payflow-agent", description="Investigates PayFlow's alerts.")
    commands = parser.add_subparsers(dest="command", required=True)
    investigate = commands.add_parser("investigate", help="investigate an alert that is firing now")
    investigate.add_argument("alert", nargs="?", help="the alert's name, e.g. ProcessorCircuitOpen")
    investigate.add_argument("--replay", metavar="TRACE", type=Path,
                             help="run again from the tool results recorded in this trace file")
    show = commands.add_parser("show", help="print the report of a past investigation")
    show.add_argument("investigation", help="'latest', an investigation id, or a trace file")
    listing = commands.add_parser("list", help="recent investigations, newest first")
    listing.add_argument("-n", type=int, default=10, help="how many (default 10)")
    evaluate = commands.add_parser("eval", help="replay the recorded scenarios and score the results")
    evaluate.add_argument("--label", default="eval", help="a word for this run, e.g. baseline (part of the results' file name)")
    evaluate.add_argument("--scenario", action="append", metavar="NAME", help="only this scenario (may be repeated)")
    arguments = parser.parse_args(argv)

    if arguments.command == "eval":
        return _eval(arguments.label, arguments.scenario)
    if arguments.command == "list":
        return _list(arguments.n)
    if arguments.command == "show":
        return _show(arguments.investigation)
    if (arguments.alert is None) == (arguments.replay is None):
        parser.error("give an alert name, or --replay with a trace file (one of the two)")
    return _investigate(arguments)


def _investigate(arguments) -> int:
    try:
        if arguments.replay:
            trace = asyncio.run(investigate_replay(arguments.replay, config.RUNS_DIR))
        else:
            trace = asyncio.run(investigate_live(arguments.alert, config.MCP_URL, config.RUNS_DIR))
    except NotFiring as e:
        print(e)
        return NOT_FIRING
    except NoRunbook as e:
        print(e)
        return NO_RUNBOOK
    except (InvestigationError, InvalidRunbook, ReplayMismatch, ModelUnavailable, ModelError, FileNotFoundError) as e:
        print(f"The investigation could not be carried out: {e}")
        return FAILED
    except BaseException as e:
        if not _refused_connection(e):
            raise
        print(f"The MCP server does not answer at {config.MCP_URL}. Start it with: python -m mcp_server.server")
        return FAILED
    print(report.render(trace.document))
    print(f"report  {trace.report_path}")
    print(f"trace   {trace.path}")
    return DONE


def _eval(label: str, only: list[str] | None) -> int:
    """Exit code 0 when every scenario passed, 1 when some did not, 2 when the eval could not run."""
    try:
        scenarios = scenario.load_all()
        if only:
            unknown = sorted(set(only) - {item.name for item in scenarios})
            if unknown:
                print(f"No scenario {unknown}. Scenarios: {', '.join(item.name for item in scenarios)}")
                return FAILED
            scenarios = [item for item in scenarios if item.name in only]
        results = asyncio.run(run_eval.run_eval(scenarios, runs.local_model(), label))
    except (scenario.InvalidScenario, InvalidRunbook, ModelUnavailable) as e:
        print(f"The eval could not be run: {e}")
        return FAILED
    print(run_eval.render(results))
    print(f"results  {run_eval.RESULTS_DIR / (results['id'] + '.md')}")
    return DONE if results["totals"]["passed"] == results["totals"]["scenarios"] else NOT_FIRING


def _show(which: str) -> int:
    path = _find(which)
    if path is None:
        print(f"No investigation {which} in {config.RUNS_DIR}. Try: payflow-agent list")
        return FAILED
    print(report.render(traces.load(path)))
    return DONE


def _list(how_many: int) -> int:
    paths = _traces()[:max(1, how_many)]
    if not paths:
        print(f"No investigations yet in {config.RUNS_DIR}.")
        return DONE
    for path in paths:
        print(report.one_line(traces.load(path)))
    return DONE


def _traces() -> list[Path]:
    """Newest first: an id begins with the time the investigation started."""
    return sorted(config.RUNS_DIR.glob("*.json"), reverse=True)


def _find(which: str) -> Path | None:
    if which == "latest":
        return next(iter(_traces()), None)
    given = Path(which)
    if given.is_file():
        return given
    by_id = config.RUNS_DIR / f"{given.name.removesuffix('.json').removesuffix('.md')}.json"
    return by_id if by_id.is_file() else None


def _refused_connection(error: BaseException) -> bool:
    """The MCP client reports a refused connection wrapped in exception groups, and as the
    ConnectError of whichever HTTP library it is built on (httpx2 today), hence by name."""
    if type(error).__name__ == "ConnectError":
        return True
    nested = list(getattr(error, "exceptions", ())) + [error.__cause__, error.__context__]
    return any(_refused_connection(inner) for inner in nested if inner is not None)


if __name__ == "__main__":
    sys.exit(main())
