"""The MCP server: PayFlow's facts as tools for the incident agent.

Run:  .venv\\Scripts\\python.exe -m mcp_server.server     (http://127.0.0.1:8091/mcp)
"""
from contextlib import contextmanager
from datetime import timedelta

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import config
from mcp_server.clients import alertmanager, kube, prometheus
from mcp_server.tools import alerts, health, logs, messaging, pods

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
    except (httpx.HTTPError, kube.KubeUnavailable) as e:
        why = str(e) if isinstance(e, kube.KubeUnavailable) else type(e).__name__
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


if __name__ == "__main__":
    server.run(transport="streamable-http", host=config.MCP_HOST, port=config.MCP_PORT)
