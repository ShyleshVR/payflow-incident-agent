import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx
from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import payflow
from mcp_server.tools import alerts, reconciliation

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
TODAY = NOW.date()
WEEK = [TODAY - timedelta(days=n) for n in range(1, 8)]  # 2026-10-11 ... 2026-10-05


def run(day, status="COMPLETED", started="02:00:10", finished="02:00:12", discrepancies=0, checked=100, error=None,
        trigger="SCHEDULED", on=None):
    """A run of a business day, as PayFlow lists it; by default the nightly run of the next morning."""
    on = on or day + timedelta(days=1)
    done = status != "RUNNING"
    completed = status == "COMPLETED"
    return {"runId": f"run-{day}-{started}", "businessDate": day.isoformat(), "trigger": trigger, "status": status,
            "startedAt": f"{on}T{started}", "finishedAt": f"{on}T{finished}" if done else None,
            "paymentsChecked": checked if completed else None, "matched": checked - discrepancies if completed else None,
            "pending": 0 if completed else None, "payoutsChecked": 2 if completed else None,
            "payoutsMatched": 2 if completed else None, "payoutsPending": 0 if completed else None,
            "discrepancyCount": discrepancies if completed else None, "error": error}


def week(**changed):
    """Every day reconciled clean by its nightly run, except the days given (as d11=[runs], ...)."""
    runs = {TODAY: []} | {day: [run(day)] for day in WEEK}
    for name, day_runs in changed.items():
        runs[date(2026, 10, int(name[1:]))] = day_runs
    return runs


def discrepancy(kind, payment="pay_8e29813f-a522-45e0-a8f6-32e22bf66e85", processor=13.0, ledger=12.0, detail="Processor 13.0000 USD vs ledger 12.0000 USD"):
    return {"type": kind, "paymentId": payment, "payoutId": None, "processorAmount": processor, "ledgerAmount": ledger,
            "paymentStatus": "SUCCESS", "detail": detail}


def built(runs_by_day, asked=None, discrepancies=None, now=NOW):
    shown = reconciliation.choose(runs_by_day, asked, now)
    return reconciliation.build(runs_by_day, shown, discrepancies, now)


def test_a_clean_week_shows_seven_days_the_newest_in_detail_and_no_concerns():
    result = built(week())

    assert [(day.date, day.status, day.discrepancies, day.runs) for day in result.days] == [
        (day.isoformat(), "clean", 0, 1) for day in WEEK]
    assert result.detail.model_dump() == {
        "date": "2026-10-11", "status": "clean", "trigger": "SCHEDULED", "finished_hours_ago": 7.0,
        "payments_checked": 100, "payments_matched": 100, "payments_pending": 0,
        "payouts_checked": 2, "payouts_matched": 2, "payouts_pending": 0,
        "discrepancies": 0, "by_type": [], "examples_read": True, "failed_runs": 0, "last_error": None,
        "running_for_minutes": None}
    assert result.hours_since_last_completed_run == 7.0
    assert result.concerns == []


def test_a_day_with_discrepancies_is_shown_in_detail_although_newer_days_are_clean():
    found = [discrepancy("SETTLEMENT_AMOUNT_MISMATCH", f"pay_{n}") for n in range(5)] + [
        discrepancy("CAPTURED_NOT_SETTLED", "pay_x", 40.0, None, "Captured auth_1 at 2026-10-08T11:00:01, no settlement in the ledger")]
    runs = week(d08=[run(date(2026, 10, 8), discrepancies=6)])

    assert reconciliation.choose(runs, None, NOW) == date(2026, 10, 8)
    assert reconciliation.run_to_read(runs[date(2026, 10, 8)]) == "run-2026-10-08-02:00:10"
    result = built(runs, discrepancies=found)

    assert [day.status for day in result.days] == ["clean", "clean", "clean", "discrepancies", "clean", "clean", "clean"]
    assert (result.detail.date, result.detail.discrepancies, result.detail.payments_matched) == ("2026-10-08", 6, 94)
    assert [(kind.type, kind.count, len(kind.examples)) for kind in result.detail.by_type] == [
        ("SETTLEMENT_AMOUNT_MISMATCH", 5, 3), ("CAPTURED_NOT_SETTLED", 1, 1)]
    assert result.detail.by_type[1].examples[0].model_dump() == {
        "id": "pay_x", "processor_amount": 40.0, "ledger_amount": None, "status": "SUCCESS",
        "detail": "Captured auth_1 at 2026-10-08T11:00:01, no settlement in the ledger"}
    assert result.concerns == [
        "2026-10-08: 6 discrepancy(ies) in its latest completed run: 5 SETTLEMENT_AMOUNT_MISMATCH, 1 CAPTURED_NOT_SETTLED"]


def test_a_day_is_judged_by_its_latest_completed_run_whatever_ran_before_or_failed_after():
    day = date(2026, 10, 9)
    fixed = week(d09=[run(day, discrepancies=3), run(day, started="14:00:00", finished="14:00:02", trigger="MANUAL")])
    failed_later = week(d09=[run(day), run(day, "FAILED", started="15:00:00", finished="15:00:01", error="503 from ledger-service")])

    failed_earlier = week(d09=[run(day, "FAILED", started="01:00:00", finished="01:00:01", error="ledger was down"), run(day)])

    assert built(fixed).days[2].status == "clean"
    assert built(fixed).concerns == []
    # a failure that a later completed run made up for is counted, but its error is not shown as the day's
    made_up_for = built(failed_earlier, asked=day)
    assert (made_up_for.detail.failed_runs, made_up_for.detail.last_error, made_up_for.concerns) == (1, None, [])
    result = built(failed_later, asked=day)
    assert (result.detail.status, result.detail.failed_runs, result.detail.last_error) == ("clean", 1, "503 from ledger-service")
    assert result.concerns == ["2026-10-09: a run started after its last completed one failed: 503 from ledger-service"]


def test_days_without_a_completed_run_are_failed_running_or_not_reconciled():
    d10, d09, _d08 = date(2026, 10, 10), date(2026, 10, 9), date(2026, 10, 8)
    error = "org.springframework.web.client.ResourceAccessException: I/O error on GET request " + "x" * 300
    runs = week(
        d10=[run(d10, "FAILED", started=f"{hour:02d}:21:00", finished=f"{hour:02d}:21:05", error=error) for hour in (4, 5, 6)],
        d09=[run(d09, "RUNNING", started="07:20:00", on=TODAY)],
        d08=[])

    result = built(runs)

    assert [(day.date, day.status, day.discrepancies) for day in result.days[1:4]] == [
        ("2026-10-10", "failed", None), ("2026-10-09", "running", None), ("2026-10-08", "not_reconciled", None)]
    assert (result.detail.date, result.detail.failed_runs, result.detail.payments_checked) == ("2026-10-10", 3, None)
    assert len(result.detail.last_error) == reconciliation.MAX_TEXT
    assert result.concerns == [
        f"2026-10-10 is not reconciled: its 3 run(s) failed, the last with: {result.detail.last_error}",
        "2026-10-09 is not reconciled: a run has been running for 100 minutes without completing",
        "2026-10-08 is not reconciled: no run was started for it",
    ]


def test_a_run_in_progress_is_not_a_concern_until_it_has_run_too_long_and_yesterday_is_not_due_just_after_midnight():
    # (a manual re-run of another day completed this morning, so "nothing completed for 26 hours" stays quiet)
    just_started = week(d11=[run(date(2026, 10, 11), "RUNNING", started="08:50:00", on=TODAY)],
                        d10=[run(date(2026, 10, 10)), run(date(2026, 10, 10), started="08:00:00", finished="08:00:02", on=TODAY)])
    after_midnight = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)
    not_closed_yet = week(d11=[])

    in_progress = built(just_started)
    early = built(not_closed_yet, now=after_midnight)

    assert (in_progress.days[0].status, in_progress.detail.running_for_minutes, in_progress.concerns) == ("running", 10, [])
    assert early.days[0].status == "not_due"
    assert early.concerns == []


def test_a_long_time_without_any_completed_run_is_a_concern_like_the_alert():
    stale = {day: [] for day in [TODAY, *WEEK]} | {WEEK[1]: [run(WEEK[1])]}     # last completed 2026-10-11 02:00
    nothing = {day: [] for day in [TODAY, *WEEK]}

    assert built(stale).hours_since_last_completed_run == 31.0
    assert built(stale).concerns[0] == "no reconciliation run has completed for 31 hours (the alert fires after 26)"
    assert built(nothing).hours_since_last_completed_run is None
    assert built(nothing).concerns[0] == "no reconciliation run has completed in the last 7 days"


def test_too_many_discrepancies_are_counted_but_not_listed():
    runs = week(d10=[run(date(2026, 10, 10), discrepancies=2_750_000, checked=2_750_997)])

    assert reconciliation.run_to_read(runs[date(2026, 10, 10)]) is None
    result = built(runs)

    assert (result.detail.discrepancies, result.detail.by_type, result.detail.examples_read) == (2_750_000, [], False)
    assert result.concerns == [
        "2026-10-10: 2750000 discrepancy(ies) in its latest completed run: too many to list (over 2000); read them in PayFlow"]


def test_the_day_asked_for_is_shown_even_when_clean_or_older_than_the_overview():
    old = date(2026, 9, 20)

    assert reconciliation.parse_date(" 2026-10-07 ", TODAY) == date(2026, 10, 7)
    assert reconciliation.parse_date(None, TODAY) is None
    assert reconciliation.days_to_read(TODAY, None) == [TODAY, *WEEK]
    assert reconciliation.days_to_read(TODAY, date(2026, 10, 7)) == [TODAY, *WEEK]
    assert reconciliation.days_to_read(TODAY, old) == [TODAY, *WEEK, old]
    runs = week(d08=[run(date(2026, 10, 8), discrepancies=6)]) | {old: [run(old, "FAILED", error="ledger down")]}
    assert built(runs, asked=date(2026, 10, 7)).detail.date == "2026-10-07"
    result = built(runs, asked=old)
    assert (result.detail.date, result.detail.status) == ("2026-09-20", "failed")
    assert len(result.days) == 7
    assert result.concerns[-1] == "2026-09-20 is not reconciled: its 1 run(s) failed, the last with: ledger down"


def test_a_date_that_is_not_a_date_or_is_in_the_future_is_explained():
    for text, message in (("yesterday", "date must look like 2026-10-11"), ("2026-13-40", "date must look like 2026-10-11"),
                          ("2026-10-13", "2026-10-13 is in the future; today is 2026-10-12 (UTC)")):
        try:
            reconciliation.parse_date(text, TODAY)
        except ValueError as e:
            assert message in str(e)
        else:
            raise AssertionError(f"{text} was accepted")


# ---- over MCP, with PayFlow played by a fake

def over_mcp(arguments, monkeypatch, runs_by_day, found=()):
    asked = []

    async def fake_get(path, params=None):
        asked.append((path, params))
        if isinstance(runs_by_day, Exception):
            raise runs_by_day
        if params is None:
            return {"run": {}, "discrepancies": list(found)}
        return runs_by_day[date.fromisoformat(params["date"])]

    monkeypatch.setattr(payflow, "get", fake_get)
    monkeypatch.setattr(alerts, "utc_now", lambda: NOW)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["get_reconciliation"], await client.call_tool("get_reconciliation", arguments)

    return (*asyncio.run(call()), asked)


def test_the_tool_reads_eight_days_and_then_the_discrepancies_of_the_day_it_shows(monkeypatch):
    runs = week(d08=[run(date(2026, 10, 8), discrepancies=1)])

    tool, result, asked = over_mcp({}, monkeypatch, runs, [discrepancy("SETTLEMENT_AMOUNT_MISMATCH")])

    assert tool.input_schema.get("required", []) == []
    assert result.structured_content["detail"]["date"] == "2026-10-08"
    assert result.structured_content["detail"]["by_type"][0]["type"] == "SETTLEMENT_AMOUNT_MISMATCH"
    assert len(result.structured_content["days"]) == 7
    assert sorted(params["date"] for _, params in asked[:8]) == sorted(day.isoformat() for day in [TODAY, *WEEK])
    assert asked[8] == ("/api/v1/reconciliation/runs/run-2026-10-08-02:00:10", None)


def test_a_clean_day_asked_for_by_date_needs_no_discrepancy_call(monkeypatch):
    _, result, asked = over_mcp({"date": "2026-10-06"}, monkeypatch, week(d08=[run(date(2026, 10, 8), discrepancies=1)]))

    assert result.structured_content["detail"]["date"] == "2026-10-06"
    assert len(asked) == 8
    # the bad day is still a concern, from the overview
    assert result.structured_content["concerns"] == ["2026-10-08: 1 discrepancy(ies) in its latest completed run"]


def test_a_bad_date_is_refused_before_payflow_is_asked_and_payflow_down_is_an_error_the_model_can_read(monkeypatch):
    _, bad_date, asked = over_mcp({"date": "last friday"}, monkeypatch, week())
    _, down, _ = over_mcp({}, monkeypatch, httpx.ConnectError("refused"))

    assert bad_date.is_error and "date must look like 2026-10-11" in bad_date.content[0].text
    assert asked == []
    assert down.is_error and "PayFlow did not answer" in down.content[0].text
