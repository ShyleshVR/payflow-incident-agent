import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as K8s

from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import kube
from mcp_server.tools import alerts, pods

NOW = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)


def minutes_ago(minutes):
    return NOW - timedelta(minutes=minutes)


def pod(name="payment-service-abc", **overrides):
    """A healthy pod as kube.snapshot describes it; a reboot five hours ago left one old restart."""
    return {
        "name": name, "phase": "Running", "ready": True, "restarts": 1, "waiting_reason": None,
        "waiting_message": None, "unschedulable": None,
        "last_exit": {"reason": "Error", "exit_code": 255, "at": minutes_ago(300)},
    } | overrides


def snapshot(pods_, desired=2, ready=2, kind="Deployment", warnings=()):
    return {"workload": {"kind": kind, "desired": desired, "ready": ready}, "pods": list(pods_), "warnings": list(warnings)}


# ---------------------------------------------------------------- the judgement

def test_healthy_pods_are_only_counted_and_an_old_restart_is_not_a_concern():
    result = pods.assess("payment-service", snapshot([pod("a"), pod("b")]), NOW)

    assert result.model_dump() == {
        "workload": "payment-service", "kind": "Deployment", "pods_wanted": 2, "pods_ready": 2,
        "healthy_pods": 2, "unhealthy_pods": [], "recent_warnings": [], "concerns": [],
    }


def test_a_crash_looping_pod_is_described_with_why_it_last_died():
    crashing = pod("ledger-service-x", ready=False, restarts=7, waiting_reason="CrashLoopBackOff",
                   waiting_message="back-off 2m40s restarting failed container",
                   last_exit={"reason": "Error", "exit_code": 1, "at": minutes_ago(1)})

    result = pods.assess("ledger-service", snapshot([pod("ledger-service-y"), crashing], ready=1), NOW)

    assert result.healthy_pods == 1
    assert result.unhealthy_pods[0].model_dump() == {
        "pod": "ledger-service-x", "state": "Waiting: CrashLoopBackOff", "restarts": 7,
        "last_exit": "Error (exit 1), 1 min ago", "detail": "back-off 2m40s restarting failed container",
    }
    assert result.concerns == ["1 of 2 pods are not ready", "1 pod(s): Waiting: CrashLoopBackOff"]


def test_scaled_to_zero_is_said_plainly():
    result = pods.assess("ledger-service", snapshot([], desired=0, ready=0), NOW)

    assert (result.pods_wanted, result.healthy_pods) == (0, 0)
    assert result.concerns == ["scaled to zero: no pods are wanted, so none run"]


def test_a_pod_that_cannot_be_scheduled_carries_the_schedulers_reason():
    pending = pod("payment-service-new", phase="Pending", ready=False, restarts=0, last_exit=None,
                  unschedulable="0/1 nodes are available: 1 Insufficient memory.")

    result = pods.assess("payment-service", snapshot([pod("a"), pod("b"), pending], desired=3), NOW)

    assert result.unhealthy_pods[0].state == "Pending: cannot be scheduled"
    assert result.unhealthy_pods[0].detail == "0/1 nodes are available: 1 Insufficient memory."
    assert result.concerns == ["1 of 3 pods are not ready", "1 pod(s): Pending: cannot be scheduled"]


def test_a_pod_that_died_minutes_ago_is_unhealthy_even_though_it_is_ready_again():
    restarted = pod("webhook-service-x", restarts=3, last_exit={"reason": "OOMKilled", "exit_code": 137, "at": minutes_ago(3)})

    result = pods.assess("webhook-service", snapshot([pod("a"), restarted]), NOW)

    assert result.unhealthy_pods[0].state == "Running, restarted in the last 15 minutes"
    assert result.unhealthy_pods[0].last_exit == "OOMKilled (exit 137), 3 min ago"
    assert result.concerns == ["1 pod(s): Running, restarted in the last 15 minutes"]


def test_a_pod_stuck_waiting_for_its_dependencies_is_unhealthy():
    starting = pod("payout-service-x", phase="Pending", ready=False, restarts=0, last_exit=None,
                   waiting_reason="Init:running", waiting_message="init container wait-for-dependencies has not finished")

    result = pods.assess("payout-service", snapshot([starting], desired=1, ready=0), NOW)

    assert result.unhealthy_pods[0].state == "Waiting: Init:running"
    assert result.unhealthy_pods[0].detail == "init container wait-for-dependencies has not finished"


def test_a_workload_that_does_not_exist_is_not_reported_as_healthy():
    result = pods.assess("redis", {"workload": None, "pods": [], "warnings": []}, NOW)

    assert result.kind is None
    assert result.concerns == ["Kubernetes has no workload named redis"]


def test_many_unhealthy_pods_are_all_counted_but_only_five_are_detailed():
    crashing = [pod(f"p{i}", ready=False, waiting_reason="CrashLoopBackOff") for i in range(8)]

    result = pods.assess("payment-service", snapshot(crashing, desired=8, ready=0), NOW)

    assert len(result.unhealthy_pods) == pods.MAX_UNHEALTHY_PODS
    assert result.concerns == ["8 of 8 pods are not ready", "8 pod(s): Waiting: CrashLoopBackOff"]


def test_warnings_are_the_last_five_shortened_with_their_age_and_count():
    warnings = [{"reason": "BackOff", "message": f"event {i}", "count": 1, "at": minutes_ago(20 - i)} for i in range(6)]
    warnings.append({"reason": "FailedScheduling", "message": "0/1 nodes   are available:\n" + "x" * 300, "count": 4, "at": minutes_ago(2)})

    result = pods.assess("payment-service", snapshot([pod()], warnings=warnings), NOW)

    assert len(result.recent_warnings) == 5
    assert result.recent_warnings[0] == "BackOff, 18 min ago: event 2"
    assert result.recent_warnings[-1].startswith("FailedScheduling x4, 2 min ago: 0/1 nodes are available: xxx")
    assert len(result.recent_warnings[-1]) < 240 and result.recent_warnings[-1].endswith("…")


# ---------------------------------------------------------------- reading Kubernetes' objects

def container(ready=True, restarts=0, waiting=None, last_terminated=None, terminated=None, name="app"):
    return K8s(name=name, ready=ready, restart_count=restarts,
               state=K8s(waiting=waiting, running=None if waiting or terminated else K8s(), terminated=terminated),
               last_state=K8s(terminated=last_terminated))


def k8s_pod(containers, phase="Running", init_containers=(), conditions=()):
    return K8s(metadata=K8s(name="payment-service-abc"),
               status=K8s(phase=phase, container_statuses=list(containers) or None,
                          init_container_statuses=list(init_containers) or None, conditions=list(conditions)))


def test_a_running_pod_is_read_with_its_last_exit():
    died = K8s(reason="OOMKilled", exit_code=137, finished_at=minutes_ago(3))

    facts = kube._pod_facts(k8s_pod([container(restarts=2, last_terminated=died)]))

    assert facts == {
        "name": "payment-service-abc", "phase": "Running", "ready": True, "restarts": 2,
        "waiting_reason": None, "waiting_message": None, "unschedulable": None,
        "last_exit": {"reason": "OOMKilled", "exit_code": 137, "at": minutes_ago(3)},
    }


def test_a_waiting_container_gives_the_reason_kubernetes_shows():
    waiting = K8s(reason="CrashLoopBackOff", message="back-off 40s restarting failed container")

    facts = kube._pod_facts(k8s_pod([container(ready=False, restarts=5, waiting=waiting)]))

    assert (facts["ready"], facts["waiting_reason"], facts["waiting_message"]) == (
        False, "CrashLoopBackOff", "back-off 40s restarting failed container")


def test_a_pod_still_in_an_init_container_and_one_that_cannot_be_scheduled():
    init = container(name="wait-for-dependencies", ready=False)
    in_init = kube._pod_facts(k8s_pod([], phase="Pending", init_containers=[init]))
    unschedulable = kube._pod_facts(k8s_pod([], phase="Pending", conditions=[
        K8s(type="PodScheduled", status="False", reason="Unschedulable", message="0/1 nodes are available")]))

    assert (in_init["ready"], in_init["waiting_reason"]) == (False, "Init:running")
    assert in_init["waiting_message"] == "init container wait-for-dependencies has not finished"
    assert unschedulable["unschedulable"] == "0/1 nodes are available"


def test_only_recent_warnings_about_this_workload_are_kept_oldest_first():
    def event(name, minutes, reason="BackOff"):
        return K8s(involved_object=K8s(name=name), reason=reason, message="m", count=2,
                   last_timestamp=minutes_ago(minutes), event_time=None, metadata=K8s(creation_timestamp=None))

    events = [event("payment-service-abc", 5), event("ledger-service-zzz", 5), event("payment-service-abc", 90),
              event("payment-service", 10, "FailedCreate")]

    kept = kube._warnings(events, {"payment-service-abc", "payment-service"}, minutes_ago(30))

    assert [(e["reason"], e["at"]) for e in kept] == [("FailedCreate", minutes_ago(10)), ("BackOff", minutes_ago(5))]


# ---------------------------------------------------------------- the tool over MCP

def over_mcp(arguments, monkeypatch, fake_snapshot):
    asked = []

    async def snapshot_(name):
        asked.append(name)
        return fake_snapshot()

    monkeypatch.setattr(kube, "snapshot", snapshot_)
    monkeypatch.setattr(alerts, "utc_now", lambda: NOW)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["get_pod_status"], await client.call_tool("get_pod_status", arguments)

    return (*asyncio.run(call()), asked)


def test_the_tool_is_served_over_mcp_for_services_and_their_infrastructure(monkeypatch):
    tool, result, asked = over_mcp({"workload": "kafka"}, monkeypatch,
                                   lambda: snapshot([pod("kafka-0")], desired=1, ready=1, kind="StatefulSet"))

    assert all(name in str(tool.input_schema) for name in ("payment-service", "postgres", "kafka", "redis"))
    assert asked == ["kafka"]
    assert result.structured_content["kind"] == "StatefulSet"
    assert result.structured_content["concerns"] == []


def test_a_workload_outside_the_list_is_refused_before_kubernetes_is_asked(monkeypatch):
    _, result, asked = over_mcp({"workload": "jaeger"}, monkeypatch, lambda: snapshot([]))

    assert result.is_error
    assert asked == []


def test_when_kubernetes_does_not_answer_the_tool_says_so(monkeypatch):
    def unavailable():
        raise kube.KubeUnavailable("HTTP 403 Forbidden")

    _, result, _ = over_mcp({"workload": "kafka"}, monkeypatch, unavailable)

    assert result.is_error
    assert "Kubernetes did not answer" in result.content[0].text
    assert "HTTP 403 Forbidden" in result.content[0].text
