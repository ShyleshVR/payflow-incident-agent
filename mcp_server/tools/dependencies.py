"""What PayFlow depends on but does not run: the card processor (called by payment-service,
behind a circuit breaker) and the bank (called by payout-service). From Prometheus."""
from pydantic import BaseModel, Field

DEFAULT_MINUTES = 15
MAX_MINUTES = 120

# The same share as get_service_health's 5xx rule.
UNANSWERED_PERCENT = 1.0

# partner -> (the service that calls it, the timer it records each call in)
PARTNERS = {
    "processor": ("payment-service", "processor_calls"),
    "bank": ("payout-service", "bank_calls"),
}

# How a call ended, as each client tags it. Both clients mean the same four things.
_OUTCOMES = {
    "SUCCEEDED": "succeeded", "OK": "succeeded",
    "DECLINED": "declined",
    "REJECTED": "rejected",
    "UNKNOWN": "unanswered",
}

_BREAKER = 'resilience4j_circuitbreaker_state{name="processor"}'
# worst first
_STATES = ("open", "forced_open", "half_open", "disabled", "metrics_only", "closed")

Rows = list[tuple[dict[str, str], float]]


def clamp_minutes(minutes: int) -> int:
    return max(1, min(MAX_MINUTES, minutes))


def queries(minutes: int) -> dict[str, str]:
    window = f"{minutes}m"
    found = {
        # one row per state, counting the payment-service instances whose breaker is in it
        "breaker_states": f"sum by (state) ({_BREAKER})",
        # open lasts only 10 seconds at a time: a single reading can miss it
        "breaker_opened": f'max(max_over_time(resilience4j_circuitbreaker_state{{name="processor",state=~"open|forced_open|half_open"}}[{window}]))',
        "breaker_refused": f'sum(increase(resilience4j_circuitbreaker_not_permitted_calls_total{{name="processor"}}[{window}]))',
        "breaker_failure_rate": 'max(resilience4j_circuitbreaker_failure_rate{name="processor"})',
    }
    for partner, (caller, timer) in PARTNERS.items():
        count = f'{timer}_seconds_count{{job="{caller}"}}'
        found |= {
            f"{partner}_caller_up": f'sum(up{{job="{caller}"}})',
            f"{partner}_watched_before": f'count(up{{job="{caller}"}} offset {window})',
            # A counter appears at its first event, already above zero, and increase() cannot see
            # that first value. So: the rise of the series that existed when the window began ...
            f"{partner}_calls": f"sum by (operation, outcome) (increase({count}[{window}]) and {count} offset {window})",
            # ... plus the whole value of those that appeared since (a first failure, a new pod).
            f"{partner}_calls_new": f"sum by (operation, outcome) ({count} unless {count} offset {window})",
            # Only right when Prometheus has no history at the window's start (see _calls).
            f"{partner}_calls_plain": f"sum by (operation, outcome) (increase({count}[{window}]))",
            f"{partner}_seconds": f'sum by (operation) (increase({timer}_seconds_sum{{job="{caller}"}}[{window}]))',
        }
    return found


class CircuitBreaker(BaseModel):
    state: str = Field(description="closed: calls go through. open: calls are refused without being sent. "
                                   "half_open: a few trial calls are testing the processor. The worst state of any instance")
    instances: int = Field(description="payment-service instances; each has its own breaker")
    instances_not_closed: int
    opened_in_window: bool = Field(description="It was open or half-open at some point in the window, or refused calls")
    calls_refused: int = Field(description="Calls refused in the window because the breaker was open")
    recent_failure_percent: int | None = Field(description="Of the last 20 calls; None: fewer than 10 calls so far")


class Operation(BaseModel):
    operation: str
    calls: int
    succeeded: int
    declined: int
    rejected: int
    unanswered: int
    avg_ms: int | None


class Partner(BaseModel):
    called_by: str
    calls: int = Field(description="Calls in the window; 0 means nobody called, not that the partner is healthy")
    succeeded: int
    declined: int = Field(description="A business 'no' (card declined, account not accepted): the partner is working")
    rejected: int = Field(description="The partner said our request was wrong: a bug or a misconfiguration")
    unanswered: int = Field(description="No answer to trust: timeout, connection failure, 5xx, or refused by the open circuit breaker")
    unanswered_percent: float
    avg_ms: int | None
    operations: list[Operation] = Field(description="The same, per operation that was called")


class DependencyStatus(BaseModel):
    minutes: int = Field(description="The window the calls were counted over")
    processor: Partner
    processor_circuit_breaker: CircuitBreaker | None = Field(description="None: payment-service reports no breaker")
    bank: Partner
    concerns: list[str] = Field(description="What looks wrong in these numbers; empty when nothing does")


def assess(minutes: int, values: dict[str, Rows]) -> DependencyStatus:
    """The numbers, plus the concerns a person would read off them. A decline is never a concern:
    a declined card is the processor doing its job."""
    concerns = []
    breaker = _breaker(values)
    if breaker is None:
        if _number(values["processor_caller_up"]):  # when it is down, that is said below, once
            concerns.append("payment-service is up but reports no circuit breaker for the processor")
    elif breaker.state in ("open", "forced_open"):
        concerns.append(f"the processor circuit breaker is open on {breaker.instances_not_closed} of {breaker.instances} "
                        f"payment-service instance(s): calls to the processor are refused without being sent"
                        + _refused(breaker, minutes))
    elif breaker.state == "half_open":
        concerns.append(f"the processor circuit breaker is half-open on {breaker.instances_not_closed} of {breaker.instances} "
                        "payment-service instance(s): trial calls are testing whether the processor is back"
                        + _refused(breaker, minutes))
    elif breaker.state != "closed":
        concerns.append(f"the processor circuit breaker is {breaker.state}: it is not protecting calls to the processor")
    elif breaker.opened_in_window:
        concerns.append(f"the processor circuit breaker opened during the last {minutes} minutes and is closed again now"
                        + _refused(breaker, minutes))

    partners = {}
    for name, (caller, _) in PARTNERS.items():
        partner = partners[name] = _partner(name, caller, values)
        if not _number(values[f"{name}_caller_up"]):
            concerns.append(f"{caller}, the only caller of the {name}, has no instance up: nothing can reach the {name}")
        if partner.calls and partner.unanswered_percent >= UNANSWERED_PERCENT:
            concerns.append(f"{partner.unanswered} of {partner.calls} calls to the {name} ({partner.unanswered_percent:g}%) got no "
                            f"answer to trust in the last {minutes} minutes ({_per_operation(partner, 'unanswered')})")
        if partner.rejected:
            concerns.append(f"the {name} rejected {partner.rejected} request(s) as wrong ({_per_operation(partner, 'rejected')}): "
                            "a bug or a misconfiguration on PayFlow's side")

    return DependencyStatus(minutes=minutes, processor=partners["processor"], processor_circuit_breaker=breaker,
                            bank=partners["bank"], concerns=concerns)


def _breaker(values: dict[str, Rows]) -> CircuitBreaker | None:
    in_state = {metric.get("state", ""): int(count) for metric, count in values["breaker_states"] if count > 0}
    if not in_state:
        return None
    instances = sum(in_state.values())
    refused = round(_number(values["breaker_refused"]) or 0)
    failure_rate = _number(values["breaker_failure_rate"])
    state = next(state for state in (*_STATES, *in_state) if state in in_state)
    return CircuitBreaker(
        state=state,
        instances=instances,
        instances_not_closed=instances - in_state.get("closed", 0),
        opened_in_window=bool(_number(values["breaker_opened"])) or refused > 0,
        calls_refused=refused,
        # the breaker reports -1 until it has seen its minimum number of calls
        recent_failure_percent=None if failure_rate is None or failure_rate < 0 else round(failure_rate),
    )


def _partner(name: str, caller: str, values: dict[str, Rows]) -> Partner:
    # the average uses the plain rise of both the time and the count, so the two match
    seconds = {metric.get("operation", ""): value for metric, value in values[f"{name}_seconds"]}
    timed: dict[str, float] = {}
    for metric, value in values[f"{name}_calls_plain"]:
        timed[metric.get("operation", "")] = timed.get(metric.get("operation", ""), 0.0) + value
    by_operation: dict[str, dict[str, int]] = {}
    for (operation, outcome), calls in _calls(name, values).items():
        ended = by_operation.setdefault(operation, dict.fromkeys(("succeeded", "declined", "rejected", "unanswered"), 0))
        # an outcome this code does not know is not a trusted answer either
        ended[_OUTCOMES.get(outcome, "unanswered")] += calls

    operations = []
    for operation, ended in sorted(by_operation.items()):
        calls = sum(ended.values())
        if calls:
            operations.append(Operation(operation=operation, calls=calls,
                                        avg_ms=_average_ms(seconds.get(operation), timed.get(operation)), **ended))

    calls = sum(operation.calls for operation in operations)
    unanswered = sum(operation.unanswered for operation in operations)
    return Partner(
        called_by=caller,
        calls=calls,
        succeeded=sum(operation.succeeded for operation in operations),
        declined=sum(operation.declined for operation in operations),
        rejected=sum(operation.rejected for operation in operations),
        unanswered=unanswered,
        unanswered_percent=round(100 * unanswered / calls, 1) if calls else 0.0,
        avg_ms=_average_ms(sum(seconds.values()), sum(timed.values())),
        operations=operations,
    )


def _calls(name: str, values: dict[str, Rows]) -> dict[tuple[str, str], int]:
    """Calls in the window per (operation, outcome). With history at the window's start, a series
    that is missing there is new, and all of it happened inside the window. Without history
    (Prometheus restarted) every series looks new, so only the plain rise can be trusted."""
    if _number(values[f"{name}_watched_before"]):
        rows = values[f"{name}_calls"] + values[f"{name}_calls_new"]
    else:
        rows = values[f"{name}_calls_plain"]
    calls: dict[tuple[str, str], float] = {}
    for metric, value in rows:
        key = (metric.get("operation", ""), metric.get("outcome", ""))
        calls[key] = calls.get(key, 0.0) + value
    # increase() extrapolates between scrapes: 3 calls can read as 3.02
    return {key: round(value) for key, value in calls.items()}


def _average_ms(seconds: float | None, calls: float | None) -> int | None:
    return None if not calls or seconds is None else round(1000 * seconds / calls)


def _per_operation(partner: Partner, ending: str) -> str:
    return ", ".join(f"{operation.operation} {getattr(operation, ending)}"
                     for operation in partner.operations if getattr(operation, ending))


def _refused(breaker: CircuitBreaker, minutes: int) -> str:
    return f"; it refused {breaker.calls_refused} call(s) in the last {minutes} minutes" if breaker.calls_refused else ""


def _number(rows: Rows) -> float | None:
    return rows[0][1] if rows else None
