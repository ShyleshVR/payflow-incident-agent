"""One workload's pods, from Kubernetes: what metrics can't tell apart (crash-looping, scaled to
zero, unschedulable, stuck starting)."""
from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

# The 9 services and the infrastructure they depend on. The schema lists these names.
Workload = Literal[
    "api-gateway", "payment-service", "ledger-service", "merchant-service", "webhook-service",
    "notification-service", "payout-service", "reconciliation-service", "processor-simulator",
    "postgres", "kafka", "redis",
]

# A restart count alone misleads (every pod restarts when the machine reboots): what matters is
# whether a container died recently.
RECENT_RESTART = timedelta(minutes=15)
MAX_UNHEALTHY_PODS = 5
MAX_WARNINGS = 5
MAX_MESSAGE = 200


class UnhealthyPod(BaseModel):
    pod: str
    state: str = Field(description="e.g. 'Waiting: CrashLoopBackOff', 'Pending: cannot be scheduled', 'Running, not ready'")
    restarts: int
    last_exit: str | None = Field(description="Why and when its container last died, e.g. 'OOMKilled (exit 137), 3 min ago'")
    detail: str | None = Field(description="Kubernetes' own message for this state")


class PodStatus(BaseModel):
    workload: str
    kind: str | None = Field(description="Deployment or StatefulSet; None: no such workload exists")
    pods_wanted: int
    pods_ready: int
    healthy_pods: int = Field(description="Running, ready, and not restarted in the last 15 minutes")
    unhealthy_pods: list[UnhealthyPod] = Field(description="Details for pods that are not healthy (at most 5)")
    recent_warnings: list[str] = Field(description="Kubernetes Warning events of the last 30 minutes, oldest first")
    concerns: list[str] = Field(description="What looks wrong; empty when nothing does")


def assess(name: str, snapshot: dict, now: datetime) -> PodStatus:
    workload = snapshot["workload"]
    wanted = workload["desired"] if workload else 0
    ready = workload["ready"] if workload else 0

    unhealthy, states = [], Counter()
    for pod in snapshot["pods"]:
        state = _state(pod, now)
        if state is None:
            continue
        states[state] += 1
        unhealthy.append(UnhealthyPod(
            pod=pod["name"], state=state, restarts=pod["restarts"], last_exit=_last_exit(pod, now),
            detail=_shorten(pod["unschedulable"] or pod["waiting_message"]),
        ))

    concerns = []
    if workload is None:
        concerns.append(f"Kubernetes has no workload named {name}")
    elif wanted == 0:
        concerns.append("scaled to zero: no pods are wanted, so none run")
    elif ready < wanted:
        concerns.append(f"{wanted - ready} of {wanted} pods are not ready")
    for state, count in states.items():
        concerns.append(f"{count} pod(s): {state}")

    return PodStatus(
        workload=name,
        kind=workload["kind"] if workload else None,
        pods_wanted=wanted,
        pods_ready=ready,
        healthy_pods=len(snapshot["pods"]) - len(unhealthy),
        unhealthy_pods=unhealthy[:MAX_UNHEALTHY_PODS],
        recent_warnings=[_warning(event, now) for event in snapshot["warnings"][-MAX_WARNINGS:]],
        concerns=concerns,
    )


def _state(pod: dict, now: datetime) -> str | None:
    """What is wrong with this pod, or None for a healthy one."""
    if pod["unschedulable"]:
        return "Pending: cannot be scheduled"
    if pod["waiting_reason"]:
        return f"Waiting: {pod['waiting_reason']}"
    if pod["phase"] != "Running":
        return pod["phase"]
    if not pod["ready"]:
        return "Running, not ready"
    last_exit = pod["last_exit"]
    if last_exit and last_exit["at"] and now - last_exit["at"] <= RECENT_RESTART:
        return "Running, restarted in the last 15 minutes"
    return None


def _last_exit(pod: dict, now: datetime) -> str | None:
    last_exit = pod["last_exit"]
    if last_exit is None:
        return None
    text = f"{last_exit['reason']} (exit {last_exit['exit_code']})"
    return f"{text}, {_ago(last_exit['at'], now)}" if last_exit["at"] else text


def _warning(event: dict, now: datetime) -> str:
    times = f" x{event['count']}" if event["count"] > 1 else ""
    return f"{event['reason']}{times}, {_ago(event['at'], now)}: {_shorten(event['message'])}"


def _ago(at: datetime, now: datetime) -> str:
    minutes = max(0, round((now - at).total_seconds() / 60))
    return f"{minutes} min ago" if minutes < 120 else f"{round(minutes / 60)} h ago"


def _shorten(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 1] + "…"
