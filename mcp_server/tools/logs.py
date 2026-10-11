"""A workload's recent WARN and ERROR log lines, grouped: four hundred copies of one failure
become one entry with a count."""
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

# The 9 services share Spring Boot's log format; Kafka and Postgres each have their own.
LogWorkload = Literal[
    "api-gateway", "payment-service", "ledger-service", "merchant-service", "webhook-service",
    "notification-service", "payout-service", "reconciliation-service", "processor-simulator",
    "kafka", "postgres",
]

DEFAULT_MINUTES = 15
MAX_MINUTES = 120
MAX_GROUPS = 10
MAX_MESSAGE = 300

# Warnings known to mean nothing, dropped before grouping. Keep this list short and exact: an
# entry here hides every matching line from every investigation.
KNOWN_HARMLESS = (
    re.compile(r"^SpringDoc /v3/api-docs endpoint is enabled by default"),
    re.compile(r"^SpringDoc /swagger-ui\.html endpoint is enabled by default"),
)


class LogGroup(BaseModel):
    level: Literal["ERROR", "WARN"]
    logger: str = Field(description="The class or component that logged it")
    message: str = Field(description="One example of the lines in this group")
    cause: str | None = Field(description="The deepest 'Caused by' of its stack trace (for Postgres, the DETAIL line)")
    count: int = Field(description="How many lines like this in the window")
    pods: int = Field(description="How many pods logged it")
    last_seen_minutes_ago: int
    in_crashed_container: bool = Field(description="True: logged by a container instance that has since died")


class RecentErrors(BaseModel):
    workload: str
    minutes: int = Field(description="The window that was read")
    pods_read: int
    error_lines: int
    warning_lines: int
    groups: list[LogGroup] = Field(description="Errors first, then the most frequent (at most 10)")
    concerns: list[str] = Field(description="What looks wrong; empty when nothing does")


# ---------------------------------------------------------------- reading the three formats

# 2026-10-11T04:21:47.489Z  WARN 1 --- [webhook-service] [ebhook-worker-1] [trace-span] c.s.w.Impl  : message
_SPRING = re.compile(r"^(?P<at>\d{4}-\d\d-\d\dT[\d:.]+Z)\s+(?P<level>[A-Z]+)\s+\d+ --- "
                     r"\[[^\]]*\] \[[^\]]*\] \[[^\]]*\] (?P<logger>\S+)\s*: (?P<message>.*)$")
# [2026-10-11 06:19:09,705] INFO [SnapshotEmitter id=1] message (org.apache.kafka.image.publisher.SnapshotEmitter)
_KAFKA = re.compile(r"^\[(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(?P<millis>\d{3})\] (?P<level>[A-Z]+) "
                    r"(?P<message>.*?)(?: \((?P<logger>[\w.$]+)\))?$")
# 2026-10-11 04:21:27.019 UTC [261] FATAL:  connection to client lost
_POSTGRES = re.compile(r"^(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) UTC \[\d+\] (?P<level>[A-Z]+):\s+(?P<message>.*)$")

_LEVELS = {"ERROR": "ERROR", "FATAL": "ERROR", "PANIC": "ERROR", "WARN": "WARN", "WARNING": "WARN"}
_EXCEPTION_LINE = re.compile(r"^[\w.$]+(?:Exception|Error)\b")


def parse(workload: str, text: str) -> Iterator[dict]:
    """The WARN and ERROR records of one log: {"level", "at", "logger", "message", "cause"}."""
    if workload == "postgres":
        return _parse_postgres(text)
    return _parse_java(_KAFKA if workload == "kafka" else _SPRING, text)


def _parse_java(pattern: re.Pattern, text: str) -> Iterator[dict]:
    record, trace = None, []
    for line in text.splitlines():
        match = pattern.match(line)
        if match is None:
            trace.append(line)  # a stack trace line: belongs to the record before it
            continue
        if record:
            yield record | {"cause": _deepest_cause(trace)}
        record, trace = None, []
        level = _LEVELS.get(match["level"])
        if level:
            at = match["at"] if pattern is _SPRING else f"{match['at']}.{match['millis']}+00:00"
            record = {"level": level, "at": datetime.fromisoformat(at),
                      "logger": match["logger"] or "kafka", "message": match["message"]}
    if record:
        yield record | {"cause": _deepest_cause(trace)}


def _deepest_cause(trace: list[str]) -> str | None:
    """The last 'Caused by' is where the failure started; without one, the exception itself."""
    causes = [line.removeprefix("Caused by: ") for line in trace if line.startswith("Caused by: ")]
    if causes:
        return causes[-1].strip()
    return next((line.strip() for line in trace if _EXCEPTION_LINE.match(line)), None)


def _parse_postgres(text: str) -> Iterator[dict]:
    record = None
    for line in text.splitlines():
        match = _POSTGRES.match(line)
        if match is None:
            continue
        if match["level"] == "DETAIL" and record:
            record["cause"] = match["message"]  # Postgres' own explanation of the line before
            continue
        if match["level"] in ("STATEMENT", "HINT", "CONTEXT"):
            continue
        if record:
            yield record
        level = _LEVELS.get(match["level"])
        record = None if level is None else {
            "level": level, "at": datetime.fromisoformat(match["at"] + "+00:00"),
            "logger": "postgres", "message": match["message"], "cause": None}
    if record:
        yield record


# ---------------------------------------------------------------- grouping

# Order matters: ids and timestamps contain digits, so they go before plain numbers.
_MASKS = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<id>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?Z?"), "<time>"),
    (re.compile(r"\b[a-z]{2,8}_[0-9a-f]{12,}\b"), "<id>"),          # pay_…, auth_…, po_…
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<address>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
)


def template(message: str) -> str:
    """The message with its variable parts replaced, so repeats of one failure compare equal."""
    for pattern, placeholder in _MASKS:
        message = pattern.sub(placeholder, message)
    return message


def summarise(workload: str, minutes: int, pod_logs: list[dict], now: datetime) -> RecentErrors:
    """pod_logs: kube.logs()' result. Lines older than the window or known harmless are dropped."""
    window_start = now - timedelta(minutes=minutes)
    groups: dict[tuple, dict] = {}
    for pod_log in pod_logs:
        for record in parse(workload, pod_log["text"]):
            if record["at"] < window_start or any(p.search(record["message"]) for p in KNOWN_HARMLESS):
                continue
            key = (record["level"], record["logger"], template(record["message"]))
            group = groups.setdefault(key, {"example": record, "count": 0, "pods": set(),
                                            "last": record["at"], "crashed": False})
            group["count"] += 1
            group["pods"].add(pod_log["pod"])
            group["last"] = max(group["last"], record["at"])
            group["crashed"] = group["crashed"] or pod_log["crashed"]
            if record["cause"] and not group["example"]["cause"]:
                group["example"] = record  # prefer showing a line that came with a stack trace

    ordered = sorted(groups.values(), key=lambda g: (g["example"]["level"] != "ERROR", -g["count"], -g["last"].timestamp()))
    errors = sum(g["count"] for g in ordered if g["example"]["level"] == "ERROR")
    warnings = sum(g["count"] for g in ordered) - errors

    concerns = []
    if errors:
        kinds = sum(1 for g in ordered if g["example"]["level"] == "ERROR")
        concerns.append(f"{errors} ERROR line(s) of {kinds} kind(s) in the last {minutes} minutes")
    if any(g["crashed"] for g in ordered):
        concerns.append("a container died in this window: lines marked in_crashed_container are from the instance that died")
    if len(ordered) > MAX_GROUPS:
        concerns.append(f"{len(ordered)} different kinds of line; only the first {MAX_GROUPS} are listed")

    return RecentErrors(
        workload=workload,
        minutes=minutes,
        pods_read=len({pod_log["pod"] for pod_log in pod_logs}),
        error_lines=errors,
        warning_lines=warnings,
        groups=[LogGroup(
            level=g["example"]["level"],
            logger=g["example"]["logger"],
            message=_shorten(g["example"]["message"]),
            cause=None if g["example"]["cause"] is None else _shorten(g["example"]["cause"]),
            count=g["count"],
            pods=len(g["pods"]),
            last_seen_minutes_ago=max(0, round((now - g["last"]).total_seconds() / 60)),
            in_crashed_container=g["crashed"],
        ) for g in ordered[:MAX_GROUPS]],
        concerns=concerns,
    )


def clamp_minutes(minutes: int) -> int:
    return max(1, min(MAX_MINUTES, minutes))


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 1] + "…"

