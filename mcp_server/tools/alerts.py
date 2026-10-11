"""Alerts, compacted for a small model: only the fields an investigation needs."""
from datetime import UTC, datetime

from pydantic import BaseModel, Field

MAX_ALERTS = 20

# Labels that say which part of the system an alert is about. Everything else Prometheus adds
# (instance addresses, pod hashes, the rule's own bookkeeping) is noise for the model.
SUBJECT_LABELS = ("application", "job", "consumergroup", "topic", "type", "pod")

_SEVERITY_ORDER = {"critical": 0, "warning": 1}


# Typed results: the server publishes their schema with the tool, and clients get structured data.
class Alert(BaseModel):
    alert: str = Field(description="The alert rule's name, e.g. KafkaConsumerLag")
    severity: str = Field(description="critical (act now) or warning (degraded or at risk)")
    about: dict[str, str] = Field(description="Which service, consumer group, topic or pod it concerns")
    firing_for_minutes: int
    summary: str


class ActiveAlerts(BaseModel):
    count: int = Field(description="How many alerts are firing (the list below holds at most 20)")
    alerts: list[Alert]


def compact(raw_alerts: list[dict], now: datetime) -> ActiveAlerts:
    """Critical alerts first, then the longest-firing; at most MAX_ALERTS of them."""
    alerts = []
    for raw in raw_alerts:
        labels = raw.get("labels", {})
        started = datetime.fromisoformat(raw["startsAt"])
        alerts.append(Alert(
            alert=labels.get("alertname", "unknown"),
            severity=labels.get("severity", "unknown"),
            about={name: labels[name] for name in SUBJECT_LABELS if name in labels},
            firing_for_minutes=max(0, round((now - started).total_seconds() / 60)),
            summary=raw.get("annotations", {}).get("summary", ""),
        ))
    alerts.sort(key=lambda a: (_SEVERITY_ORDER.get(a.severity, 2), -a.firing_for_minutes))
    return ActiveAlerts(count=len(alerts), alerts=alerts[:MAX_ALERTS])


def utc_now() -> datetime:
    return datetime.now(UTC)
