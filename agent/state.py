"""What one investigation knows as it moves through the workflow.

Every step reads this and returns only the keys it changes; LangGraph merges them in.
"""
from typing import Literal, TypedDict

Status = Literal["investigating", "not_firing", "completed"]


class Alert(TypedDict):
    """One firing alert, as the list_active_alerts tool reports it."""
    alert: str
    severity: str
    about: dict[str, str]
    firing_for_minutes: int
    summary: str


class Investigation(TypedDict, total=False):
    alert_name: str           # what was asked for
    status: Status
    # Every firing alert with that name. One name with several label sets (a service, a topic)
    # is one incident, so one investigation carries them all.
    alerts: list[Alert]
    firing_now: list[str]     # the names of everything firing, to say so when the alert asked for is not
    runbook: dict             # the alert's runbook: guidance for the model, and the checks it may use
    # Gathering: what has been found so far, keyed by the check's name (each check runs once).
    evidence: dict[str, dict]
    rounds: int               # rounds of checks run so far
    next_checks: list[str]    # what the model chose for the round about to run
    gathering: dict           # set when gathering is over: how many rounds, and why it stopped
    # The cause, as far as verified evidence supports it: root_cause, confidence, summary, the
    # citations found in the evidence and those that were not; and set_aside, the text in the
    # evidence that addressed the agent or the operator: not a fact, never acted on, shown in the report.
    diagnosis: dict
    result: dict              # what the report step concluded
