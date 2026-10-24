"""The MCP server: PayFlow's facts as tools for the incident agent.

Run:  .venv\\Scripts\\python.exe -m mcp_server.server     (http://127.0.0.1:8091/mcp)
"""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import config
from mcp_server.clients import alertmanager, kube, payflow, prometheus
from mcp_server.tools import (
    alerts,
    dependencies,
    health,
    logs,
    messaging,
    payouts,
    pods,
    reconciliation,
    sagas,
)

server = MCPServer(
    "payflow",
    instructions="Read-only facts about the PayFlow payments system, for investigating its alerts.",
)


@contextmanager
def reaching(source: str, url: str):
    """A source that doesn't answer becomes a ToolError: its message reaches the model, while any
    other exception's text is hidden from clients."""
    try:
        yield
    except (httpx.HTTPError, kube.KubeUnavailable, payflow.PayflowUnavailable) as e:
        why = type(e).__name__ if isinstance(e, httpx.HTTPError) else str(e)
        raise ToolError(f"{source} did not answer at {url} ({why}). This fact is unavailable; it "
                        "says nothing about PayFlow itself.") from e


# Each docstring is the tool's description: it is what the model reads to decide when to call it.

@server.tool()
async def list_active_alerts() -> alerts.ActiveAlerts:
    """Lists the alerts firing in PayFlow right now: name, severity, what each is about, how long
    it has been firing and its one-line summary. Critical alerts come first. Call this first in
    any investigation, and again later to see whether an alert has cleared."""
    with reaching("Alertmanager", config.ALERTMANAGER_URL):
        raw = await alertmanager.firing_alerts()
    return alerts.compact(raw, alerts.utc_now())


@server.tool()
async def get_service_health(service: health.Service) -> health.ServiceHealth:
    """Health of one PayFlow service over the last 5 minutes: instances up, time since the last
    start, request rate, share of 5xx errors, average response time, requests waiting for a
    database connection and CPU. `concerns` lists what looks wrong; an empty list means these
    numbers look healthy. Use it to check a service an alert points at, or one it depends on."""
    with reaching("Prometheus", config.PROMETHEUS_URL):
        values = await prometheus.instant(health.queries(service))
    return health.assess(service, values)


@server.tool()
async def get_pod_status(workload: pods.Workload) -> pods.PodStatus:
    """Kubernetes' view of one workload's pods: how many are wanted and ready, and for each pod
    that is not healthy its state (CrashLoopBackOff, cannot be scheduled, not ready, recently
    restarted), why its container last died, and recent Warning events. Covers the services and
    postgres, kafka and redis. Use it when a service has instances down or keeps restarting, or to
    check a dependency. `concerns` lists what looks wrong; empty means the pods look healthy."""
    with reaching("Kubernetes", f"namespace {config.NAMESPACE}"):
        snapshot = await kube.snapshot(workload)
    return pods.assess(workload, snapshot, alerts.utc_now())


@server.tool()
async def get_recent_errors(workload: logs.LogWorkload, minutes: int = logs.DEFAULT_MINUTES) -> logs.RecentErrors:
    """The WARN and ERROR lines a workload logged recently, grouped: each group has one example
    message, the root cause from its stack trace, how many times and on how many pods it was
    logged, and how long ago. Errors come first. Covers the services, kafka and postgres.
    `minutes` is how far back to read (default 15, at most 120): widen it for an alert that has
    been firing longer. Use it to find out why a service is failing once you know which one."""
    minutes = logs.clamp_minutes(minutes)
    with reaching("Kubernetes", f"namespace {config.NAMESPACE}"):
        pod_logs = await kube.logs(workload, timedelta(minutes=minutes))
    return logs.summarise(workload, minutes, pod_logs, alerts.utc_now())


@server.tool()
async def get_messaging_status(minutes: int = messaging.DEFAULT_MINUTES) -> messaging.MessagingStatus:
    """Whether messages are flowing between the services: whether Kafka can be reached, how far
    each consumer is behind on each topic (lag) and whether anyone is reading it, each service's
    outbox (messages waiting to be published, the oldest one's age, parked messages), and the
    dead-letter topics that received messages. `change` compares with `minutes` ago (default 15,
    at most 120), so a backlog that is draining can be told from one that is growing. Use it
    for alerts about lag, outbox backlog or dead letters, and when payments or payouts are slow
    although every service is up. `concerns` lists what looks wrong; empty means messages flow."""
    minutes = messaging.clamp_minutes(minutes)
    with reaching("Prometheus", config.PROMETHEUS_URL):
        values = await prometheus.vectors(messaging.queries(minutes))
    return messaging.assess(minutes, values)


@server.tool()
async def get_dependency_status(minutes: int = dependencies.DEFAULT_MINUTES) -> dependencies.DependencyStatus:
    """The two outside partners PayFlow depends on: the card processor (called by payment-service)
    and the bank (called by payout-service). For each: how many calls were made in the last
    `minutes` (default 15, at most 120) and how they ended, in total and per operation:
    succeeded, declined (a business "no"; the partner is working), rejected (our request was
    wrong) or unanswered (timeout, connection failure, 5xx), with the average time. Also the
    processor circuit breaker: its state now, whether it opened in the window and how many
    calls it refused. Use it for alerts about the circuit breaker, stuck saga steps or failing
    payouts, to tell a partner outage from a PayFlow fault. `concerns` lists what looks wrong."""
    minutes = dependencies.clamp_minutes(minutes)
    with reaching("Prometheus", config.PROMETHEUS_URL):
        values = await prometheus.vectors(dependencies.queries(minutes))
    return dependencies.assess(minutes, values)


# Saga views read at once by list_parked_sagas: up to 50 are needed, without crowding PayFlow.
_PARALLEL_READS = 8


async def _or_none(call):
    """One of several sources not answering must not hide what the others say."""
    try:
        return await call
    except httpx.HTTPError:
        return None


@server.tool()
async def list_sagas_needing_attention() -> sagas.SagasNeedingAttention:
    """The payment, refund and payout sagas that need a look right now. A saga is PayFlow's
    step-by-step record of one money movement. Two kinds are listed: parked (it stopped at an
    outcome that must not be guessed, such as an unanswered capture, and waits for an operator's
    retry) and stuck (still running, but over 15 minutes in the same step). Each has the id of
    its payment or payout, the step it stopped in, for how long, and its last error. Use it for
    the alerts SagaRequiresAttention and SagaStepStuck, or when money movements hang; then call
    get_saga with an id from the list. `concerns` lists what needs attention; empty means none."""
    with reaching("PayFlow", config.PAYFLOW_URL):
        payments, payouts = await asyncio.gather(_or_none(payflow.get(sagas.PAYMENTS_LIST, sagas.LIST_PARAMS)),
                                                 _or_none(payflow.get(sagas.PAYOUTS_LIST, sagas.LIST_PARAMS)))
    if payments is None and payouts is None:
        raise ToolError(f"Neither payment-service nor payout-service answered through PayFlow's gateway at "
                        f"{config.PAYFLOW_URL}. Whether sagas need attention is unknown; check the gateway and both services.")
    return sagas.summarise(payments, payouts)


@server.tool()
async def get_saga(id: str) -> sagas.SagaDetail:
    """One payment's or payout's saga in detail: its state, the step it stopped in and why (last
    error, failure code), its most recent step outcomes with their times, and the status and
    amount the merchant sees. `id` is a payment id (pay_...) or a payout id (po_...), as
    list_sagas_needing_attention returns them. For a payment with a refund, the unfinished saga
    is shown. Use it to find out why one money movement is parked or stuck."""
    try:
        saga_path, lookup_path, lookup_body = sagas.paths(id)
    except ValueError as e:
        raise ToolError(str(e)) from e
    with reaching("PayFlow", config.PAYFLOW_URL):
        try:
            saga_view = await payflow.get(saga_path)
        except payflow.NotFound as e:
            raise ToolError(f"PayFlow has no payment or payout with the id {id}.") from e
        except payflow.BadRequest as e:
            raise ToolError(f"PayFlow does not accept {id} as an id: after pay_ or po_ it must be the full id, "
                            "exactly as list_sagas_needing_attention returns it.") from e
        # the status is a bonus: the saga is still worth showing without it
        looked_up = await _or_none(payflow.post(lookup_path, lookup_body)) or []
    try:
        return sagas.detail(id, saga_view, looked_up, alerts.utc_now())
    except LookupError as e:
        raise ToolError(str(e)) from e


@server.tool()
async def list_parked_sagas(service: Literal["payment-service", "payout-service"]) -> sagas.ParkedSagas:
    """Every parked saga of one service (up to 50, the oldest first), each with the code it parked
    for, such as CAPTURE_UNKNOWN (no answer from the processor) or HOLD_REJECTED (the ledger said
    no). For the agent's own code, which sorts them into those that may be retried and those that
    need a person; not one of a runbook's checks. Read-only, like every tool here."""
    with reaching("PayFlow", config.PAYFLOW_URL):
        answer = await payflow.get(sagas.LISTS[service], sagas.PARKED_LIST_PARAMS)
        limit = asyncio.Semaphore(_PARALLEL_READS)

        async def view(item: dict):
            saga_path = sagas.paths(item["payoutId"] if service == "payout-service" else item["paymentId"])[0]
            async with limit:
                try:
                    return await _or_none(payflow.get(saga_path))
                except (payflow.NotFound, payflow.BadRequest):
                    return None        # this saga's code stays unknown; the others are still worth reading

        views = await asyncio.gather(*(view(item) for item in sagas.parked_to_read(service, answer)))
    return sagas.parked(service, answer, list(views))


@server.tool()
async def list_failed_reconciliations() -> reconciliation.FailedDays:
    """Every business day of the last week (today included) that no reconciliation run has
    reconciled, and whose newest run failed: its failed runs and the last one's error, which names
    the source that did not answer. For the agent's own code, which decides which days may be run
    again; not one of a runbook's checks. Read-only, like every tool here."""
    now = alerts.utc_now()
    days = reconciliation.days_to_read(now.date(), None)
    with reaching("PayFlow", config.PAYFLOW_URL):
        answers = await asyncio.gather(*(payflow.get(reconciliation.RUNS, {"date": day.isoformat()}) for day in days))
    return reconciliation.failed(dict(zip(days, answers, strict=True)), now)


@server.tool()
async def get_failed_payouts(hours: int = payouts.DEFAULT_HOURS) -> payouts.FailedPayouts:
    """The payouts (money sent from a merchant's balance to its bank account) that failed or came
    back from the bank in the last `hours` (default 1, at most 48), grouped by status and failure
    code, with a few examples each, and how many were paid meanwhile. invalid_account,
    account_closed and account_frozen are the bank's answer about the merchant's account;
    insufficient_payable_balance is PayFlow's ledger. Use it for the alert PayoutsFailing.
    `concerns` lists what looks wrong; empty means none failed or came back."""
    window = payouts.clamp_hours(hours)
    with reaching("PayFlow", config.PAYFLOW_URL):
        listed = await payflow.get(payouts.PAYOUTS, {"limit": payouts.READ})
    return payouts.summarise(listed, window, alerts.utc_now())


@server.tool()
async def get_reconciliation(date: str | None = None) -> reconciliation.Reconciliation:
    """PayFlow's daily reconciliation, which compares the card processor and the bank, the ledger
    and the statuses told to merchants for each business day (UTC), and reports every
    disagreement as a discrepancy. Returns the last 7 days at a glance (clean, discrepancies,
    failed, running, not reconciled) and one day in detail: its counts, each discrepancy type
    with its count and up to 3 examples (payment or payout id, both amounts, status, what is
    wrong), and the error of a failed run. `date` (YYYY-MM-DD) picks the day shown in detail;
    without it, the newest day that is not clean. Use it for the alerts
    ReconciliationDiscrepancies, ReconciliationRunFailed and ReconciliationNotRun. `concerns`
    lists what looks wrong; empty means every day is reconciled and clean."""
    now = alerts.utc_now()
    try:
        asked = reconciliation.parse_date(date, now.date())
    except ValueError as e:
        raise ToolError(str(e)) from e
    days = reconciliation.days_to_read(now.date(), asked)
    with reaching("PayFlow", config.PAYFLOW_URL):
        answers = await asyncio.gather(*(payflow.get(reconciliation.RUNS, {"date": day.isoformat()}) for day in days))
        runs_by_day = dict(zip(days, answers, strict=True))
        shown = reconciliation.choose(runs_by_day, asked, now)
        run_id = reconciliation.run_to_read(runs_by_day[shown])
        discrepancies = None if run_id is None else (await payflow.get(f"{reconciliation.RUNS}/{run_id}"))["discrepancies"]
    return reconciliation.build(runs_by_day, shown, discrepancies, now)


if __name__ == "__main__":
    server.run(transport="streamable-http", host=config.MCP_HOST, port=config.MCP_PORT)
