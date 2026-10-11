"""The Kubernetes API, read-only: one workload, its pods, their warning events and their logs.

Returns plain dicts, so the judgement in tools/pods.py can be tested without Kubernetes objects.
The official client is blocking; calls run in a worker thread.
"""
import asyncio
import os
from datetime import UTC, datetime, timedelta

import urllib3
from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client.exceptions import ApiException

from mcp_server import config

WARNING_WINDOW = timedelta(minutes=30)
# per pod, so an error storm can't make one tool call read an unbounded log
LOG_TAIL_LINES = 5000


class KubeUnavailable(Exception):
    """The Kubernetes API did not answer, or refused the request."""


_core: client.CoreV1Api | None = None
_apps: client.AppsV1Api | None = None


def _apis() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    global _core, _apps
    if _core is None:
        # in the cluster: the pod's ServiceAccount; on a developer machine: the kubeconfig
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            kube_config.load_incluster_config()
        else:
            kube_config.load_kube_config()
        _core, _apps = client.CoreV1Api(), client.AppsV1Api()
    return _core, _apps


async def snapshot(name: str, namespace: str | None = None) -> dict:
    """{"workload": {...} or None, "pods": [...], "warnings": [...]} for the workload `name`,
    whose pods carry the label app=<name>."""
    return await asyncio.to_thread(_snapshot, name, namespace or config.NAMESPACE)


def _snapshot(name: str, namespace: str) -> dict:
    try:
        core, apps = _apis()
        workload = _workload(apps, name, namespace)
        pods = core.list_namespaced_pod(namespace, label_selector=f"app={name}").items
        events = core.list_namespaced_event(namespace, field_selector="type=Warning").items
    except (ApiException, urllib3.exceptions.HTTPError, kube_config.ConfigException) as e:
        reason = f"HTTP {e.status} {e.reason}" if isinstance(e, ApiException) else type(e).__name__
        raise KubeUnavailable(reason) from e

    pod_names = {pod.metadata.name for pod in pods}
    return {
        "workload": workload,
        "pods": [_pod_facts(pod) for pod in pods],
        "warnings": _warnings(events, pod_names | {name}, datetime.now(UTC) - WARNING_WINDOW),
    }


async def logs(name: str, since: timedelta, namespace: str | None = None) -> list[dict]:
    """The recent log text of each pod of the workload: [{"pod", "crashed", "text"}, ...].
    A pod whose container died inside the window also gives the log of the instance that died
    ("crashed": True): a crash-looping container's own log often shows nothing but its startup."""
    return await asyncio.to_thread(_logs, name, since, namespace or config.NAMESPACE)


def _logs(name: str, since: timedelta, namespace: str) -> list[dict]:
    try:
        core, _ = _apis()
        pods = core.list_namespaced_pod(namespace, label_selector=f"app={name}").items
        texts = []
        for pod in pods:
            container = pod.spec.containers[0].name
            for crashed in _instances_to_read(pod, datetime.now(UTC) - since):
                text = _read_log(core, pod.metadata.name, namespace, container, since, previous=crashed)
                if text is not None:
                    texts.append({"pod": pod.metadata.name, "crashed": crashed, "text": text})
        return texts
    except (ApiException, urllib3.exceptions.HTTPError, kube_config.ConfigException) as e:
        reason = f"HTTP {e.status} {e.reason}" if isinstance(e, ApiException) else type(e).__name__
        raise KubeUnavailable(reason) from e


def _instances_to_read(pod, window_start: datetime) -> list[bool]:
    """False: the running container. True as well: the one before it, if it died in the window."""
    exits = [status.last_state.terminated.finished_at for status in pod.status.container_statuses or []
             if status.last_state and status.last_state.terminated and status.last_state.terminated.finished_at]
    return [False, True] if any(at >= window_start for at in exits) else [False]


def _read_log(core: client.CoreV1Api, pod: str, namespace: str, container: str, since: timedelta,
              previous: bool) -> str | None:
    try:
        return core.read_namespaced_pod_log(
            pod, namespace, container=container, previous=previous,
            since_seconds=max(1, int(since.total_seconds())), tail_lines=LOG_TAIL_LINES)
    except ApiException as e:
        if e.status == 400:
            # no log to read yet: the container is still being created, or has no earlier instance
            return None
        raise


def _workload(apps: client.AppsV1Api, name: str, namespace: str) -> dict | None:
    """Services are Deployments; Postgres and Kafka are StatefulSets."""
    for kind, read in (("Deployment", apps.read_namespaced_deployment),
                       ("StatefulSet", apps.read_namespaced_stateful_set)):
        try:
            found = read(name, namespace)
        except ApiException as e:
            if e.status == 404:
                continue
            raise
        return {"kind": kind, "desired": found.spec.replicas or 0, "ready": found.status.ready_replicas or 0}
    return None


def _pod_facts(pod) -> dict:
    containers = pod.status.container_statuses or []
    init_containers = pod.status.init_container_statuses or []

    waiting_reason = waiting_message = None
    for status in containers:
        if status.state and status.state.waiting:
            waiting_reason, waiting_message = status.state.waiting.reason, status.state.waiting.message
            break
    else:
        # a pod still in its init containers (PayFlow's wait for their dependencies) has no
        # waiting app container yet
        for status in init_containers:
            done = status.state and status.state.terminated and status.state.terminated.exit_code == 0
            if not done:
                stuck = status.state.waiting.reason if status.state and status.state.waiting else "running"
                waiting_reason = f"Init:{stuck}"
                waiting_message = f"init container {status.name} has not finished"
                break

    exits = [status.last_state.terminated for status in containers
             if status.last_state and status.last_state.terminated]
    last_exit = max(exits, key=lambda t: t.finished_at or datetime.min.replace(tzinfo=UTC), default=None)

    unschedulable = next((condition.message or condition.reason for condition in pod.status.conditions or []
                          if condition.type == "PodScheduled" and condition.status == "False"), None)

    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "ready": bool(containers) and all(status.ready for status in containers),
        "restarts": sum(status.restart_count for status in containers),
        "waiting_reason": waiting_reason,
        "waiting_message": waiting_message,
        "last_exit": None if last_exit is None else {
            "reason": last_exit.reason, "exit_code": last_exit.exit_code, "at": last_exit.finished_at},
        "unschedulable": unschedulable,
    }


def _warnings(events, names: set[str], since: datetime) -> list[dict]:
    recent = []
    for event in events:
        at = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
        if event.involved_object.name in names and at and at >= since:
            recent.append({"reason": event.reason, "message": event.message or "", "count": event.count or 1, "at": at})
    return sorted(recent, key=lambda e: e["at"])
