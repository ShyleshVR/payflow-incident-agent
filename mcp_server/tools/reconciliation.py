"""PayFlow's daily reconciliation: the recent days at a glance, and one day in detail. From
PayFlow's API, read with the incident-agent client.

Every night PayFlow compares what the card processor (and the bank) did, what its ledger booked
and what it told merchants, for the business day that just ended (UTC). Each disagreement is a
discrepancy. As PayFlow's own alert does, a day is judged by its latest *completed* run: a run
that failed afterwards does not erase a result, and a clean day does not hide a bad one before it.
"""
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

RUNS = "/api/v1/reconciliation/runs"

# The window of PayFlow's ReconciliationDiscrepancies alert.
OVERVIEW_DAYS = 7
# ReconciliationNotRun fires after this long without a completed run.
NOT_RUN_HOURS = 26
# A day can be reconciled once it is over plus PayFlow's matching margin.
CLOSES_AFTER = timedelta(days=1, hours=1)
# A large day takes about half an hour; a run with nothing completed after this long may be stuck.
RUNNING_TOO_LONG_MINUTES = 60
EXAMPLES_PER_TYPE = 3
# PayFlow returns a run's discrepancies as one list. Beyond this, only the count is reported.
MAX_DISCREPANCIES_READ = 2000
MAX_TEXT = 200

Status = Literal["clean", "discrepancies", "failed", "running", "not_reconciled", "not_due"]


class Day(BaseModel):
    date: str
    status: Status = Field(description="clean / discrepancies: by its latest completed run. failed, running: no run has "
                                       "completed. not_reconciled: no run at all. not_due: the day only just ended")
    discrepancies: int | None = Field(description="Of its latest completed run; None: no completed run")
    runs: int


class Example(BaseModel):
    id: str | None = Field(description="The payment (pay_...) or payout (po_...); get_saga takes it")
    processor_amount: float | None = Field(description="What the processor (or, for a payout, the bank) says")
    ledger_amount: float | None
    status: str | None = Field(description="The payment's or payout's status")
    detail: str | None


class DiscrepancyType(BaseModel):
    type: str
    count: int
    examples: list[Example]


class DayDetail(BaseModel):
    date: str
    status: Status
    trigger: str | None = Field(description="Of the latest completed run: SCHEDULED or MANUAL")
    finished_hours_ago: float | None = Field(description="When the latest completed run finished")
    payments_checked: int | None
    payments_matched: int | None
    payments_pending: int | None = Field(description="Skipped because their saga was still running")
    payouts_checked: int | None
    payouts_matched: int | None
    payouts_pending: int | None
    discrepancies: int | None
    by_type: list[DiscrepancyType] = Field(description="Most frequent first; empty when there are none, or they were not read")
    examples_read: bool = Field(description="False: too many discrepancies to list; only the count is known")
    failed_runs: int = Field(description="Runs of this day that failed")
    last_error: str | None = Field(description="Of the most recent failed run, unless a run completed after it")
    running_for_minutes: int | None = Field(description="A run of this day is in progress")


class Reconciliation(BaseModel):
    days: list[Day] = Field(description="The last 7 finished business days (UTC), newest first")
    detail: DayDetail = Field(description="The day asked for, or else the newest day that is not clean")
    hours_since_last_completed_run: float | None = Field(description="Of any day; None: none in the days read")
    concerns: list[str] = Field(description="What looks wrong; empty when every day is reconciled and clean")


def parse_date(text: str | None, today: date) -> date | None:
    if text is None or not text.strip():
        return None
    try:
        asked = date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"date must look like {today - timedelta(days=1)} (a business day, UTC); got {text!r}") from None
    if asked > today:
        raise ValueError(f"{asked} is in the future; today is {today} (UTC)")
    return asked


def days_to_read(today: date, asked: date | None) -> list[date]:
    """Today (a manual run may have reconciled it), the overview's days, and the day asked for."""
    days = [today - timedelta(days=n) for n in range(OVERVIEW_DAYS + 1)]
    return days if asked is None or asked in days else [*days, asked]


def choose(runs_by_day: dict[date, list[dict]], asked: date | None, now: datetime) -> date:
    """The day shown in detail: the one asked for, else the newest finished day that is not clean,
    else the newest finished day."""
    if asked is not None:
        return asked
    finished = _overview_days(now)
    return next((day for day in finished if _status(day, runs_by_day[day], now) not in ("clean", "not_due")), finished[0])


def run_to_read(runs: list[dict]) -> str | None:
    """The run whose discrepancies are worth fetching, if any."""
    completed = _latest_completed(runs)
    count = (completed or {}).get("discrepancyCount") or 0
    return completed["runId"] if 0 < count <= MAX_DISCREPANCIES_READ else None


def build(runs_by_day: dict[date, list[dict]], shown: date, discrepancies: list[dict] | None, now: datetime) -> Reconciliation:
    """discrepancies: those of run_to_read(runs of the day shown), or None when there was nothing to read."""
    finished = _overview_days(now)
    days = [Day(date=day.isoformat(), status=_status(day, runs_by_day[day], now),
                discrepancies=(_latest_completed(runs_by_day[day]) or {}).get("discrepancyCount"),
                runs=len(runs_by_day[day])) for day in finished]
    detail = _detail(shown, runs_by_day[shown], discrepancies, now)

    concerns = []
    completions = [run["finishedAt"] for runs in runs_by_day.values() for run in runs if run["status"] == "COMPLETED"]
    since_completed = None if not completions else round(_hours_between(max(completions), now), 1)
    if since_completed is None:
        concerns.append(f"no reconciliation run has completed in the last {OVERVIEW_DAYS} days")
    elif since_completed > NOT_RUN_HOURS:
        concerns.append(f"no reconciliation run has completed for {since_completed:g} hours (the alert fires after {NOT_RUN_HOURS})")
    for day in sorted({*finished, shown}, reverse=True):
        concerns.extend(_day_concerns(day, runs_by_day[day], detail if day == shown else None, now))

    return Reconciliation(days=days, detail=detail, hours_since_last_completed_run=since_completed, concerns=concerns)


def _overview_days(now: datetime) -> list[date]:
    today = now.date()
    return [today - timedelta(days=n) for n in range(1, OVERVIEW_DAYS + 1)]


def _status(day: date, runs: list[dict], now: datetime) -> Status:
    completed = _latest_completed(runs)
    if completed is not None:
        return "discrepancies" if (completed.get("discrepancyCount") or 0) > 0 else "clean"
    if not runs:
        closes = datetime.combine(day, datetime.min.time()) + CLOSES_AFTER
        return "not_due" if now.replace(tzinfo=None) < closes else "not_reconciled"
    return "running" if _newest(runs)["status"] == "RUNNING" else "failed"


def _detail(day: date, runs: list[dict], discrepancies: list[dict] | None, now: datetime) -> DayDetail:
    completed = _latest_completed(runs) or {}
    failed = [run for run in runs if run["status"] == "FAILED"]
    # a failure that a later completed run made up for explains nothing about the day's result
    unresolved = [run for run in failed if not completed or run["startedAt"] > completed["startedAt"]]
    running = [run for run in runs if run["status"] == "RUNNING"]
    count = completed.get("discrepancyCount")
    return DayDetail(
        date=day.isoformat(),
        status=_status(day, runs, now),
        trigger=completed.get("trigger"),
        finished_hours_ago=None if not completed else round(_hours_between(completed["finishedAt"], now), 1),
        payments_checked=completed.get("paymentsChecked"),
        payments_matched=completed.get("matched"),
        payments_pending=completed.get("pending"),
        payouts_checked=completed.get("payoutsChecked"),
        payouts_matched=completed.get("payoutsMatched"),
        payouts_pending=completed.get("payoutsPending"),
        discrepancies=count,
        by_type=_by_type(discrepancies or []),
        examples_read=not count or discrepancies is not None,
        failed_runs=len(failed),
        last_error=None if not unresolved else _shorten(_newest(unresolved).get("error")),
        running_for_minutes=None if not running else int(_hours_between(_newest(running)["startedAt"], now) * 60),
    )


def _by_type(discrepancies: list[dict]) -> list[DiscrepancyType]:
    counts = Counter(item["type"] for item in discrepancies)
    return [DiscrepancyType(type=kind, count=count, examples=[
        Example(id=item.get("paymentId") or item.get("payoutId"), processor_amount=item.get("processorAmount"),
                ledger_amount=item.get("ledgerAmount"), status=item.get("paymentStatus"), detail=_shorten(item.get("detail")))
        for item in discrepancies if item["type"] == kind][:EXAMPLES_PER_TYPE]) for kind, count in counts.most_common()]


def _day_concerns(day: date, runs: list[dict], detail: DayDetail | None, now: datetime) -> list[str]:
    """detail: given for the day shown in detail, whose discrepancy types are known."""
    status = _status(day, runs, now)
    completed = _latest_completed(runs)
    newest = _newest(runs) if runs else None
    concerns = []
    if status == "discrepancies":
        types = ""
        if detail is not None and detail.by_type:
            types = ": " + ", ".join(f"{kind.count} {kind.type}" for kind in detail.by_type)
        elif detail is not None and not detail.examples_read:
            types = f": too many to list (over {MAX_DISCREPANCIES_READ}); read them in PayFlow"
        concerns.append(f"{day}: {completed['discrepancyCount']} discrepancy(ies) in its latest completed run{types}")
    elif status == "failed":
        concerns.append(f"{day} is not reconciled: its {len(runs)} run(s) failed, the last with: {_shorten(newest.get('error'))}")
    elif status == "not_reconciled":
        concerns.append(f"{day} is not reconciled: no run was started for it")
    elif status == "running":
        minutes = int(_hours_between(newest["startedAt"], now) * 60)
        if minutes > RUNNING_TOO_LONG_MINUTES:
            concerns.append(f"{day} is not reconciled: a run has been running for {minutes} minutes without completing")
    if completed is not None and newest["status"] == "FAILED" and newest["startedAt"] > completed["startedAt"]:
        concerns.append(f"{day}: a run started after its last completed one failed: {_shorten(newest.get('error'))}")
    return concerns


def _latest_completed(runs: list[dict]) -> dict | None:
    completed = [run for run in runs if run["status"] == "COMPLETED"]
    return max(completed, key=lambda run: run["finishedAt"]) if completed else None


def _newest(runs: list[dict]) -> dict:
    return max(runs, key=lambda run: run["startedAt"])


def _hours_between(timestamp: str, now: datetime) -> float:
    """PayFlow's timestamps are UTC without a zone."""
    then = datetime.fromisoformat(timestamp).replace(tzinfo=None)
    return max(0.0, (now.replace(tzinfo=None) - then).total_seconds() / 3600)


def _shorten(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"
