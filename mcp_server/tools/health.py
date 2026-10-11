"""One service's health, from its Prometheus metrics."""
from typing import Literal

from pydantic import BaseModel, Field

# The schema lists these, so the model can only name a service that exists.
Service = Literal[
    "api-gateway", "payment-service", "ledger-service", "merchant-service", "webhook-service",
    "notification-service", "payout-service", "reconciliation-service", "processor-simulator",
]

# Health probes and metric scrapes are most of a service's requests; they would hide real traffic.
_REAL_REQUESTS = 'uri!~"/actuator.*"'
_WINDOW = "5m"


def queries(service: str) -> dict[str, str]:
    """Every query reduces to one number for this service (see prometheus.instant)."""
    job = f'job="{service}"'
    requests = f"http_server_requests_seconds_count{{{job},{_REAL_REQUESTS}}}"
    return {
        "instances_up": f"sum(up{{{job}}})",
        "instances_expected": f"count(up{{{job}}})",
        "uptime_seconds": f"min(process_uptime_seconds{{{job}}})",
        "requests_per_second": f"sum(rate({requests}[{_WINDOW}]))",
        "server_errors_per_second":
            f'sum(rate(http_server_requests_seconds_count{{{job},{_REAL_REQUESTS},outcome="SERVER_ERROR"}}[{_WINDOW}]))',
        "avg_response_seconds":
            f"sum(rate(http_server_requests_seconds_sum{{{job},{_REAL_REQUESTS}}}[{_WINDOW}])) / sum(rate({requests}[{_WINDOW}]))",
        "db_connections_waiting": f"max(hikaricp_connections_pending{{{job}}})",
        "cpu_cores_used": f"sum(process_cpu_usage{{{job}}} * system_cpu_count{{{job}}})",
    }


class ServiceHealth(BaseModel):
    service: str
    instances_up: int
    instances_expected: int | None = Field(description="None: Prometheus sees no instance at all")
    minutes_since_last_start: int | None = Field(description="Of the most recently started instance")
    requests_per_second: float = Field(description="Real API requests over the last 5 minutes")
    server_error_percent: float = Field(description="Share of those requests answered with a 5xx")
    avg_response_ms: int | None = Field(description="None when there were no requests")
    db_connections_waiting: int | None = Field(description="Requests waiting for a database connection; None: no database")
    cpu_cores_used: float | None
    concerns: list[str] = Field(description="What looks wrong in these numbers; empty when nothing does")


def assess(service: str, values: dict[str, float | None]) -> ServiceHealth:
    """The numbers, plus the concerns a person would read off them. The thresholds live here, in
    code, so every model sees the same judgement instead of making its own from raw numbers."""
    up = int(values["instances_up"] or 0)
    expected = None if values["instances_expected"] is None else int(values["instances_expected"])
    uptime = values["uptime_seconds"]
    rate = values["requests_per_second"] or 0.0
    error_percent = 100 * (values["server_errors_per_second"] or 0.0) / rate if rate else 0.0
    average = values["avg_response_seconds"]
    waiting = values["db_connections_waiting"]
    minutes_up = None if uptime is None else int(uptime // 60)

    concerns = []
    if expected is None:
        concerns.append("Prometheus sees no instance of this service: it is scaled to zero, or its pods never started")
    elif up == 0:
        concerns.append(f"no instance is up (0 of {expected})")
    elif up < expected:
        concerns.append(f"{expected - up} of {expected} instances are down")
    if minutes_up is not None and minutes_up < 5:
        concerns.append(f"an instance started {minutes_up} minute(s) ago: a restart or a new pod")
    if error_percent >= 1:
        concerns.append(f"{error_percent:.1f}% of requests fail with a 5xx")
    if waiting:
        concerns.append(f"{int(waiting)} request(s) are waiting for a database connection")

    return ServiceHealth(
        service=service,
        instances_up=up,
        instances_expected=expected,
        minutes_since_last_start=minutes_up,
        requests_per_second=round(rate, 2),
        server_error_percent=round(error_percent, 2),
        avg_response_ms=None if average is None else round(average * 1000),
        db_connections_waiting=None if waiting is None else int(waiting),
        cpu_cores_used=None if values["cpu_cores_used"] is None else round(values["cpu_cores_used"], 2),
        concerns=concerns,
    )
