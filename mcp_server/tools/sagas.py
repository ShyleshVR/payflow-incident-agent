"""Payment, refund and payout sagas that need a look, and one saga in detail. From PayFlow's API,
read with the incident-agent client.

A saga is PayFlow's record of one money movement, step by step. Two kinds need attention:
parked (it stopped at an outcome that must not be guessed and waits for an operator's retry) and
stuck (still running, but in the same step for too long).
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# The SagaStepStuck alert's threshold: the list is then exactly what the alert is about.
STUCK_MINUTES = 15
# Sagas listed per service and per reason (PayFlow's counts are always complete).
LIST_LIMIT = 5
STEPS_SHOWN = 10
MAX_TEXT = 200

PAYMENTS_LIST = "/api/v1/payments/sagas/attention"
PAYOUTS_LIST = "/api/v1/payouts/sagas/attention"
LIST_PARAMS = {"stuckMinutes": STUCK_MINUTES, "limit": LIST_LIMIT}

Kind = Literal["payment", "refund", "payout"]


class SagaInList(BaseModel):
    id: str = Field(description="The payment (pay_...) or payout (po_...) it belongs to; pass it to get_saga")
    kind: Kind
    reason: Literal["parked", "stuck"] = Field(description="parked: waits for an operator's retry. stuck: running, but too long in one step")
    stopped_in: str = Field(description="The step it is parked or stuck in")
    minutes: int = Field(description="Since that step began (for a parked saga this includes the time it kept retrying)")
    attempts: int
    failure_code: str | None
    last_error: str | None


class SagasNeedingAttention(BaseModel):
    stuck_after_minutes: int = Field(description="A running saga counts as stuck after this long in one step")
    payments_parked: int | None = Field(description="Payment and refund sagas; None: payment-service did not answer")
    payments_stuck: int | None
    payouts_parked: int | None = Field(description="None: payout-service did not answer")
    payouts_stuck: int | None
    sagas: list[SagaInList] = Field(description="Parked first, then the longest waiting")
    not_listed: int = Field(description="Sagas counted above but left out of the list to keep it short")
    concerns: list[str] = Field(description="What looks wrong; empty when no saga needs attention")


def summarise(payments: dict | None, payouts: dict | None) -> SagasNeedingAttention:
    """Each argument is PayFlow's answer for one service, or None when that service did not answer
    (the other one's sagas are still worth reporting)."""
    sagas = [_in_list(item, _payment_kind(item), item["paymentId"]) for item in (payments or {}).get("sagas", [])]
    sagas += [_in_list(item, "payout", item["payoutId"]) for item in (payouts or {}).get("sagas", [])]
    sagas.sort(key=lambda saga: (saga.reason != "parked", -saga.minutes, saga.id))

    concerns = []
    counted = 0
    for name, answer, kinds in (("payment", payments, ("payment", "refund")), ("payout", payouts, ("payout",))):
        if answer is None:
            concerns.append(f"{name}-service did not answer: whether its sagas need attention is unknown")
            continue
        counted += answer["parked"] + answer["stuck"]
        ours = [saga for saga in sagas if saga.kind in kinds]
        if answer["parked"]:
            concerns.append(f"{answer['parked']} {name} saga(s) are parked and wait for an operator's retry"
                            + _oldest([saga for saga in ours if saga.reason == "parked"]))
        if answer["stuck"]:
            concerns.append(f"{answer['stuck']} {name} saga(s) have been in the same step for over {STUCK_MINUTES} minutes"
                            + _oldest([saga for saga in ours if saga.reason == "stuck"]))

    return SagasNeedingAttention(
        stuck_after_minutes=STUCK_MINUTES,
        payments_parked=None if payments is None else payments["parked"],
        payments_stuck=None if payments is None else payments["stuck"],
        payouts_parked=None if payouts is None else payouts["parked"],
        payouts_stuck=None if payouts is None else payouts["stuck"],
        sagas=sagas,
        not_listed=max(0, counted - len(sagas)),
        concerns=concerns,
    )


def _in_list(item: dict, kind: Kind, owner: str) -> SagaInList:
    parked = item["reason"] == "PARKED"
    return SagaInList(
        id=owner,
        kind=kind,
        reason="parked" if parked else "stuck",
        # a parked saga's state is REQUIRES_ATTENTION; where it stopped is what matters
        stopped_in=(item.get("stuckState") if parked else None) or item["state"],
        minutes=item["minutesInStep"],
        attempts=item["attempt"],
        failure_code=item.get("failureCode"),
        last_error=_shorten(item.get("lastError")),
    )


def _payment_kind(item: dict) -> Kind:
    return "refund" if item.get("type") == "REFUND" else "payment"


def _oldest(listed: list[SagaInList]) -> str:
    if not listed:
        return ""
    oldest = max(listed, key=lambda saga: saga.minutes)
    return f"; the oldest, {oldest.id}, for {oldest.minutes} minutes in {oldest.stopped_in}"


# ---- one saga

class Step(BaseModel):
    step: str
    outcome: str
    detail: str | None
    times: int = Field(description="How many times in a row this same outcome was recorded (retries)")
    minutes_ago: int = Field(description="Of the most recent of them")


class SagaDetail(BaseModel):
    id: str
    kind: Kind
    status: str | None = Field(description="The payment's or payout's status, as the merchant sees it; None: unknown")
    amount: str | None
    state: str = Field(description="REQUIRES_ATTENTION: parked for an operator's retry")
    stopped_in: str | None = Field(description="For a parked saga: the step it stopped in, which a retry resumes")
    finished: bool
    attempts: int = Field(description="Attempts at the current step")
    minutes_since_last_step: int | None = Field(description="Since the last recorded step outcome")
    failure_code: str | None
    last_error: str | None
    steps: list[Step] = Field(description="The most recent step outcomes, oldest first; repeats of one outcome are one row")
    earlier_steps: int = Field(description="Older step outcomes left out")
    other_sagas: int = Field(description="Other, finished sagas of the same payment (e.g. the payment before its refund)")
    concerns: list[str] = Field(description="What looks wrong with this saga; empty when nothing does")


def paths(saga_id: str) -> tuple[str, str, dict]:
    """(saga view, lookup, lookup body) for a payment or payout id. The id's prefix says which
    service owns it, so the model never has to."""
    if saga_id.startswith("pay_"):
        return f"/api/v1/payments/{saga_id}/saga", "/api/v1/payments/lookup", {"paymentIds": [saga_id]}
    if saga_id.startswith("po_"):
        return f"/api/v1/payouts/{saga_id}/saga", "/api/v1/payouts/lookup", {"payoutIds": [saga_id]}
    raise ValueError("id must be a payment id (pay_...) or a payout id (po_...), as list_sagas_needing_attention returns them")


def detail(saga_id: str, saga_view: dict | list, looked_up: list[dict], now: datetime) -> SagaDetail:
    """payment-service answers with every saga of the payment, payout-service with the one saga
    a payout has. The saga shown is the unfinished one, or else the most recent."""
    all_sagas = saga_view if isinstance(saga_view, list) else [saga_view]
    if not all_sagas:
        raise LookupError(f"PayFlow has no saga for {saga_id}: it never started a money movement")
    saga = next((s for s in all_sagas if s.get("finishedAt") is None), max(all_sagas, key=lambda s: s["createdAt"]))
    kind: Kind = "payout" if saga_id.startswith("po_") else _payment_kind(saga)
    owner = looked_up[0] if looked_up else {}

    steps = saga.get("steps", [])
    shown = _merge_repeats(steps, now)[-STEPS_SHOWN:]
    finished = saga.get("finishedAt") is not None
    parked = saga["state"] == "REQUIRES_ATTENTION"
    since_last = shown[-1].minutes_ago if shown else None

    concerns = []
    if parked:
        concerns.append(f"parked in {saga.get('stuckState')} and waiting for an operator's retry"
                        + (f": {_shorten(saga['lastError'])}" if saga.get("lastError") else ""))
    elif not finished and since_last is not None and since_last >= STUCK_MINUTES:
        concerns.append(f"still running, but no step outcome for {since_last} minutes (in {saga['state']})")

    return SagaDetail(
        id=saga_id,
        kind=kind,
        status=owner.get("status"),
        amount=None if owner.get("amount") is None else f"{owner['amount']:.2f} {owner.get('currency', '')}".strip(),
        state=saga["state"],
        stopped_in=saga.get("stuckState"),
        finished=finished,
        attempts=saga.get("attempt", 0),
        minutes_since_last_step=since_last,
        failure_code=saga.get("failureCode"),
        last_error=_shorten(saga.get("lastError")),
        steps=shown,
        earlier_steps=len(steps) - sum(step.times for step in shown),
        other_sagas=len(all_sagas) - 1,
        concerns=concerns,
    )


def _merge_repeats(steps: list[dict], now: datetime) -> list[Step]:
    """A saga retrying against a partner that is down records the same outcome again and again.
    Shown one by one, those rows would push out the steps that explain how it got there."""
    merged: list[Step] = []
    for step in steps:
        row = Step(step=step["state"], outcome=step["outcome"], detail=_shorten(step.get("detail")), times=1,
                   minutes_ago=_minutes_between(step["occurredAt"], now))
        last = merged[-1] if merged else None
        if last and (last.step, last.outcome, last.detail) == (row.step, row.outcome, row.detail):
            merged[-1] = row.model_copy(update={"times": last.times + 1})
        else:
            merged.append(row)
    return merged


def _minutes_between(timestamp: str, now: datetime) -> int:
    """PayFlow's timestamps are UTC without a zone."""
    then = datetime.fromisoformat(timestamp)
    return max(0, int((now.replace(tzinfo=None) - then.replace(tzinfo=None)).total_seconds() // 60))


def _shorten(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"
