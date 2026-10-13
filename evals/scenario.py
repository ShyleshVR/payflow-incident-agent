"""The scenarios an eval runs: evals/scenarios/<name>.yaml.

A scenario is a recorded incident with a known answer. The file names the recording to replay
(a trace in evals/fixtures) and what a correct investigation must conclude: the root cause, and
the "key checks", the ones whose results actually show that cause. It also says, for a person,
how the incident was caused and cleaned up.

A safety scenario has an instruction to the agent planted in its evidence (see evals/derive.py).
Its file also says which check's result carries the instruction (expected.planted_in) and, under
expected.must_not, what the instruction asked for: a root cause to report, or words to say. None
of it may happen.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent import config, runbooks
from agent import trace as traces

EVALS_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = EVALS_DIR / "scenarios"
FIXTURES_DIR = EVALS_DIR / "fixtures"


@dataclass(frozen=True)
class Scenario:
    name: str
    alert: str
    recording: Path
    description: str
    root_causes: tuple[str, ...]   # any of these is the right answer (usually one)
    key_checks: tuple[str, ...]    # a correct diagnosis cites at least one of these
    forbidden_causes: tuple[str, ...] = ()   # a planted instruction asked for one of these to be reported
    forbidden_text: tuple[str, ...] = ()     # ... or for the model to say one of these
    planted_in: str | None = None            # the check whose result carries that instruction

    @property
    def has_planted_instruction(self) -> bool:
        return bool(self.forbidden_causes or self.forbidden_text)


class InvalidScenario(Exception):
    """A scenario file an eval must not run with."""


def load_all(scenarios_dir: Path = SCENARIOS_DIR, fixtures_dir: Path = FIXTURES_DIR,
             runbooks_dir: Path | None = None) -> list[Scenario]:
    return [load(path, fixtures_dir, runbooks_dir) for path in sorted(scenarios_dir.glob("*.yaml"))]


def load(path: Path, fixtures_dir: Path = FIXTURES_DIR, runbooks_dir: Path | None = None) -> Scenario:
    """Reads a scenario and checks it against the runbook and the recording, so that a typo in
    an expected cause fails here, with a reason, and not as a wrong score."""
    def invalid(why: str) -> InvalidScenario:
        return InvalidScenario(f"{path.name}: {why}")

    try:
        fields = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise invalid(f"not valid YAML ({e})") from e
    if not isinstance(fields, dict):
        raise invalid("it must be a set of fields")
    if fields.get("name") != path.stem:
        raise invalid(f"it says name: {fields.get('name')}, the file is named for {path.stem}")
    expected = fields.get("expected")
    if not isinstance(expected, dict) or not expected.get("root_causes") or not isinstance(expected["root_causes"], list):
        raise invalid("expected needs root_causes, a list with at least one cause")
    causes = tuple(str(cause) for cause in expected["root_causes"])
    key_checks = tuple(str(check) for check in expected.get("key_checks") or ())
    if not key_checks and causes != (runbooks.UNKNOWN,):
        raise invalid("expected needs key_checks: the checks whose results show the cause")
    must_not = expected.get("must_not", {})
    if not isinstance(must_not, dict) or set(must_not) - {"root_causes", "say"}:
        raise invalid("expected.must_not takes root_causes (causes that must not be reported) and say (words that must not be said)")
    forbidden_causes = tuple(str(cause) for cause in must_not.get("root_causes") or ())
    forbidden_text = tuple(" ".join(str(text).split()) for text in must_not.get("say") or ())
    if "must_not" in expected and not (forbidden_causes or forbidden_text) or not all(forbidden_text):
        raise invalid("expected.must_not names nothing that must not happen")
    planted_in = expected.get("planted_in")
    if ("must_not" in expected) != (planted_in is not None):
        raise invalid("expected.planted_in (the check that carries a planted instruction) and expected.must_not go together")
    if set(forbidden_causes) & set(causes):
        raise invalid(f"{sorted(set(forbidden_causes) & set(causes))} cannot be both the expected cause and one that must not be reported")

    try:
        runbook = runbooks.load(str(fields.get("alert")), runbooks_dir or config.RUNBOOKS_DIR)
    except runbooks.NoRunbook as e:
        raise invalid(str(e)) from e
    known_causes = {cause.name for cause in runbook.causes} | {runbooks.UNKNOWN}
    known_checks = {check.name for check in runbook.checks}
    if set(causes + forbidden_causes) - known_causes:
        raise invalid(f"the runbook of {runbook.alert} has no cause {sorted(set(causes + forbidden_causes) - known_causes)}")
    named_checks = set(key_checks) | ({str(planted_in)} if planted_in is not None else set())
    if named_checks - known_checks:
        raise invalid(f"the runbook of {runbook.alert} has no check {sorted(named_checks - known_checks)}")

    recording = fixtures_dir / str(fields.get("recording"))
    if not recording.is_file():
        raise invalid(f"its recording {recording.name} is not in {fixtures_dir}")
    recorded = traces.load(recording)
    if recorded.get("alert_name") != runbook.alert:
        raise invalid(f"its recording is of {recorded.get('alert_name')}, not {runbook.alert}")
    # a replay may choose any check of the runbook, so the recording must hold every one of them
    recorded_calls = {(call["tool"], _key(call["arguments"])) for step in recorded["steps"] for call in step["tool_calls"]}
    missing = [check.name for check in runbook.checks if (check.tool, _key(check.arguments)) not in recorded_calls]
    if missing:
        raise invalid(f"its recording has no result for the check(s) {missing}: record the incident again")

    return Scenario(name=fields["name"], alert=runbook.alert, recording=recording,
                    description=" ".join(str(fields.get("description", "")).split()), root_causes=causes, key_checks=key_checks,
                    forbidden_causes=forbidden_causes, forbidden_text=forbidden_text,
                    planted_in=None if planted_in is None else str(planted_in))


def _key(arguments: dict) -> str:
    return repr(sorted(arguments.items()))
