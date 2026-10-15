"""The agent's command line.

    payflow-agent investigate ProcessorCircuitOpen       # the alert must be firing now
    payflow-agent investigate --replay runs\\<id>.json    # again, from a recorded investigation
    payflow-agent list                                   # recent investigations
    payflow-agent show latest                            # the report of one (latest, an id, or a file)
    payflow-agent eval --label baseline                  # replay every recorded scenario and score it
    payflow-agent serve                                  # the service: Alertmanager's alerts, proposals
    payflow-agent pending                                # proposals waiting for your decision
    payflow-agent approve 3 --note "..."                 # decide on one (reject needs --reason)

A live investigation needs the model server (scripts\\start-llm.ps1), the MCP server
(python -m mcp_server.server) and the PayFlow cluster. A replay or an eval needs only the model server.
Listing and showing need nothing: reports are made from the trace files in runs\\.
"""
import argparse
import asyncio
import getpass
import sys
from pathlib import Path

import httpx

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
    # Redirected to a file on Windows, the output is cp1252, which lacks characters a model writes
    # (a non-breaking hyphen): printing the report then failed after the investigation had finished.
    # Such a character is replaced on screen; the report file itself is always UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
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
    evaluate.add_argument("--held-out", action="store_true",
                          help="the held-out scenarios (evals/scenarios/held-out), never used for tuning, instead of the others")
    evaluate.add_argument("--fresh", action="store_true",
                          help="the fresh scenarios (evals/scenarios/fresh), made to compare models, instead of the others")
    commands.add_parser("pending", help="investigations whose proposal waits for your decision (asks the service)")
    approve = commands.add_parser("approve", help="approve the proposal of an investigation (asks the service)")
    approve.add_argument("id", type=int, help="the service's investigation id (see: payflow-agent pending)")
    approve.add_argument("--note", default="", help="why, or anything to record with the decision")
    reject = commands.add_parser("reject", help="reject the proposal of an investigation (asks the service)")
    reject.add_argument("id", type=int)
    reject.add_argument("--reason", required=True, help="why it is rejected (required)")
    serve = commands.add_parser("serve", help="run as a service: take Alertmanager's alerts and investigate them")
    serve.add_argument("--host", default=config.AGENT_HOST, help=f"where to listen (default {config.AGENT_HOST})")
    serve.add_argument("--port", type=int, default=config.AGENT_PORT, help=f"(default {config.AGENT_PORT})")
    arguments = parser.parse_args(argv)

    if arguments.command == "eval":
        which = "fresh" if arguments.fresh else "held-out" if arguments.held_out else None
        return _eval(arguments.label if arguments.label != "eval" or not which else which, arguments.scenario, which)
    if arguments.command == "serve":
        return _serve(arguments.host, arguments.port)
    if arguments.command == "pending":
        return _pending()
    if arguments.command in ("approve", "reject"):
        return _decide(arguments.id, arguments.command, arguments.note if arguments.command == "approve" else arguments.reason)
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


def _eval(label: str, only: list[str] | None, which: str | None = None) -> int:
    """Exit code 0 when every scenario passed, 1 when some did not, 2 when the eval could not run."""
    try:
        folder = {"held-out": scenario.HELD_OUT_DIR, "fresh": scenario.FRESH_DIR}.get(which, scenario.SCENARIOS_DIR)
        scenarios = scenario.load_all(folder)
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


_refused_connection = runs.refused_connection


def _agent_client() -> httpx.Client:
    """The running service (payflow-agent serve), with its token. Replaced in tests."""
    host = "127.0.0.1" if config.AGENT_HOST in ("0.0.0.0", "") else config.AGENT_HOST
    headers = {"Authorization": f"Bearer {config.AGENT_TOKEN}"} if config.AGENT_TOKEN else {}
    return httpx.Client(base_url=f"http://{host}:{config.AGENT_PORT}", headers=headers, timeout=30)


def _ask_service(method: str, path: str, **kwargs) -> httpx.Response | None:
    try:
        with _agent_client() as client:
            return client.request(method, path, **kwargs)
    except httpx.ConnectError:
        print(f"The agent service does not answer on port {config.AGENT_PORT}. Start it with: payflow-agent serve")
        return None


def _pending() -> int:
    answer = _ask_service("GET", "/investigations", params={"status": "awaiting_decision", "limit": 100})
    if answer is None or answer.status_code != 200:
        if answer is not None:
            print(f"The service refused: {answer.status_code} {answer.text}")
        return FAILED
    waiting = answer.json()
    if not waiting:
        print("Nothing waits for a decision.")
    for item in reversed(waiting):
        print(f"{item['id']:>4}  {item['alert_name']}: {item['root_cause']}  (since {item['finished_at']})")
        print(f"      proposed: {item['proposal']}")
        print(f"      report:   runs/{item['trace_id']}.md")
    return DONE


def _decide(investigation_id: int, command: str, note: str) -> int:
    body = {"decision": command, "by": getpass.getuser(), "note": note}
    answer = _ask_service("POST", f"/investigations/{investigation_id}/decision", json=body)
    if answer is None:
        return FAILED
    if answer.status_code != 200:
        print(f"Not {'approved' if command == 'approve' else 'rejected'}: {answer.json().get('detail', answer.text)}")
        return FAILED
    decided = answer.json()
    print(f"Investigation {investigation_id} ({decided['alert_name']}: {decided['root_cause']}): {decided['decision']} by "
          f"{decided['decided_by']} at {decided['decided_at']}.")
    if decided["decision"] == "approved":
        print("Nothing is carried out: the agent does not act yet. The step is yours to take.")
    return DONE


def _serve(host: str, port: int) -> int:
    import uvicorn

    from api.app import default_app

    print(f"listening on http://{host}:{port}  (alerts: POST /alerts; investigations: GET /investigations)")
    uvicorn.run(default_app(), host=host, port=port, log_level="info")
    return DONE


if __name__ == "__main__":
    sys.exit(main())
