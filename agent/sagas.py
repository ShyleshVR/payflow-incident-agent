"""Which parked sagas the agent may propose to retry: decided by code, never by the model.

Each parked saga is sorted into a cause by the code PayFlow parked it for (the runbook's
parking_codes). Only a saga whose cause is retryable (the runbook's retryable: no answer came,
so a retry asks again under the same idempotency key) goes on the retry list. Every other one
(a partner or the ledger said no, a code nobody listed, a saga that could not be read) needs a
person, and the report says so. At most MAX_RETRIES sagas go on one list.
"""
from agent.tools import Tools

# The most sagas one proposal may retry (the user's decision, 2026-10-22).
MAX_RETRIES = 50
SERVICES = ("payment-service", "payout-service")
# The step proposed when code found sagas to retry but the diagnosis named no cause that retries them.
RETRY_STEP = ("Retry the sagas listed below: each parked because the processor or the bank gave no answer, and a retry "
              "asks again under the same idempotency key. Each is checked again just before it is retried.")
TOOL = "list_parked_sagas"


def services_of(alerts: list[dict]) -> list[str]:
    """The services the firing alerts are about (their application label); both when none says."""
    named = sorted({alert.get("about", {}).get("application") for alert in alerts} & set(SERVICES))
    return named or list(SERVICES)


async def read(services: list[str], tools: Tools) -> dict[str, dict | str]:
    """Each service's parked sagas, or why they could not be read."""
    read_ = {}
    for service in services:
        result = await tools.call(TOOL, {"service": service})
        read_[service] = result.data if not result.is_error else str(result.data)
    return read_


def sort(parked: dict[str, dict | str], runbook: dict) -> dict:
    """{"retry": [...], "needs_a_person": [...], "not_listed": n, "over_the_limit": n, "unread": {service: why}}.
    Each saga: id, kind, stopped_in, parked_for, cause (None: no cause has its code), minutes."""
    cause_of = {code: cause for cause, codes in runbook.get("parking_codes", {}).items() for code in codes}
    retryable = set(runbook.get("retryable", ()))
    retry, needs_a_person, unread, not_listed = [], [], {}, 0
    for service, answer in parked.items():
        if isinstance(answer, str):
            unread[service] = answer
            continue
        not_listed += answer["not_listed"]
        for saga in answer["sagas"]:
            row = {key: saga[key] for key in ("id", "kind", "stopped_in", "parked_for", "minutes")}
            row["cause"] = cause_of.get(saga["parked_for"])
            (retry if row["cause"] in retryable else needs_a_person).append(row)
    retry.sort(key=lambda row: (-row["minutes"], row["id"]))
    return {"retry": retry[:MAX_RETRIES], "needs_a_person": needs_a_person, "not_listed": not_listed,
            "over_the_limit": max(0, len(retry) - MAX_RETRIES), "unread": unread}
