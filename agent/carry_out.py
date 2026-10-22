"""Carrying out an approved retry list: what code checks before each retry, and how it watches.

Only the sagas proposed (the retry list a person approved) are retried, one at a time. Just
before each, code checks again (recheck): the saga is still parked, for the same code as when it
was proposed; and its partner answers now (answering). A saga that fails either is skipped, with
the reason, and the rest go on. Afterwards each retried saga is watched until it finishes or
parks again, for up to WATCH_SECONDS (watched).

Reads go through the read-only tools (the partner's calls and health) and the operator login
(the saga itself, as the retry will see it). The retry is the operator login's alone.
"""
from agent.operator import PARKED, Operator, OperatorUnavailable, Refused
from agent.tools import Tools

# The user's decisions (2026-10-22): the partner's calls over the last 2 minutes; watch for 5.
CALLS_MINUTES = 2
WATCH_SECONDS = 300
WATCH_EVERY_SECONDS = 10
# The share of 5xx that get_service_health also treats as a concern.
SERVER_ERROR_PERCENT = 1.0
# processor-simulator plays both partners in PayFlow's cluster: the card processor and the bank.
PARTNER_WORKLOAD = "processor-simulator"
PARTNER_OF = {"processor_no_answer": "processor", "bank_no_answer": "bank"}
# Breaker states in which calls to the processor are refused without being sent.
BREAKER_REFUSING = ("open", "forced_open")

# What became of one saga of the list.
RETRIED, SKIPPED, REFUSED, NOT_REACHED = "retried", "skipped", "refused", "not_reached"


async def answering(partner: str, tools: Tools) -> tuple[bool, str]:
    """Whether the processor or the bank answers now, and what says so. Its calls of the last
    CALLS_MINUTES decide: every call answered (and, for the processor, its breaker not open on any
    instance). Nobody called it: its own health decides (pods ready, up, no 5xx)."""
    status = await tools.call("get_dependency_status", {"minutes": CALLS_MINUTES})
    if status.is_error:
        return False, f"its calls could not be read: {status.data}"
    breaker = status.data.get("processor_circuit_breaker") if partner == "processor" else None
    # Open: calls are refused unsent. Half-open is not "down": it lets trial calls through, and
    # stays half-open until enough of them succeed on each instance, which without traffic can be
    # indefinitely (seen live, 2026-10-22). So half-open goes on to the calls and the health below
    # (the user's decision), and a retry is then one of those trial calls.
    if breaker and breaker["state"] in BREAKER_REFUSING:
        return False, (f"the processor circuit breaker is {breaker['state']} on {breaker['instances_not_closed']} of "
                       f"{breaker['instances']} payment-service instance(s)")
    half_open = ("; the processor circuit breaker is half-open, letting trial calls through"
                 if breaker and breaker["state"] == "half_open" else "")
    calls = status.data[partner]
    if calls["calls"]:
        if calls["unanswered"]:
            return False, (f"{calls['unanswered']} of {calls['calls']} calls to the {partner} got no answer in the last "
                           f"{CALLS_MINUTES} minutes")
        return True, f"all {calls['calls']} calls to the {partner} in the last {CALLS_MINUTES} minutes were answered{half_open}"

    pods = await tools.call("get_pod_status", {"workload": PARTNER_WORKLOAD})
    health = await tools.call("get_service_health", {"service": PARTNER_WORKLOAD})
    nobody = f"nobody called the {partner} in the last {CALLS_MINUTES} minutes, and"
    if pods.is_error or health.is_error:
        return False, f"{nobody} its health could not be read: {pods.data if pods.is_error else health.data}"
    if not pods.data["pods_wanted"] or pods.data["pods_ready"] < pods.data["pods_wanted"]:
        return False, f"{nobody} {PARTNER_WORKLOAD} has {pods.data['pods_ready']} of {pods.data['pods_wanted']} pods ready"
    if not health.data["instances_up"] or health.data["server_error_percent"] >= SERVER_ERROR_PERCENT:
        return False, (f"{nobody} {PARTNER_WORKLOAD} has {health.data['instances_up']} instance(s) up, answering "
                       f"{health.data['server_error_percent']:g}% of requests with a 5xx")
    return True, f"{nobody} {PARTNER_WORKLOAD}'s pods are all ready and it answers without 5xx{half_open}"


async def retry_one(row: dict, operator: Operator, tools: Tools) -> dict:
    """Checks one saga of the list again and, if it still qualifies, retries it. Returns what
    happened: {"id", "outcome", "why", "saga" (its sagaId, to watch it), "partner"}."""
    done = {"id": row["id"], "parked_for": row["parked_for"], "saga": None, "partner": None}
    try:
        now = await operator.saga(row["id"])
    except Refused as e:
        return done | {"outcome": SKIPPED, "why": str(e)}
    except OperatorUnavailable as e:
        return done | {"outcome": NOT_REACHED, "why": str(e)}
    if now["state"] != PARKED:
        return done | {"outcome": SKIPPED, "why": f"no longer parked: it is {now['state']}" + (" (finished)" if now["finished"] else "")}
    if now["parked_for"] != row["parked_for"]:
        return done | {"outcome": SKIPPED, "why": f"parked for {now['parked_for']} now, not {row['parked_for']} as proposed"}

    partner = PARTNER_OF.get(row["cause"])
    if partner is None:      # code never proposes such a saga; it is checked anyway
        return done | {"outcome": SKIPPED, "why": f"{row['cause']} is not a cause the agent retries"}
    answers, why = await answering(partner, tools)
    done |= {"saga": now["saga"], "partner": why}
    if not answers:
        return done | {"outcome": SKIPPED, "why": f"the {partner} does not answer yet: {why}"}

    try:
        await operator.retry(row["id"])
    except Refused as e:
        return done | {"outcome": REFUSED, "why": str(e)}
    except OperatorUnavailable as e:
        return done | {"outcome": NOT_REACHED, "why": str(e)}
    return done | {"outcome": RETRIED, "why": None}


def settled(saga: dict) -> str | None:
    """How a retried saga has ended, or None while it is still running."""
    if saga["finished"]:
        return f"finished: {saga['state']}"
    if saga["state"] == PARKED:
        return f"parked again, for {saga['parked_for']}"
    return None
