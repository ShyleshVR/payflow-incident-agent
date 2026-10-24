---
alert: ServiceDown
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", ServiceDown / InstanceDown; the alert rule in k8s/base/observability/alerts.yml
causes:
  scaled_to_zero: The service's deployment wants no pods at all, so none run (it was scaled to zero).
  crash_loop: The service's pods start and die again (CrashLoopBackOff, or containers that exited recently), from a bad setting or secret, running out of memory, or something it needs at start-up (the database, Kafka) not answering.
  not_schedulable: The service's pods are Pending, because the cluster cannot place them (not enough memory or CPU, or a constraint nothing meets).
# {job} is the service the alert is about (its job label), filled in when the investigation starts.
shown_by:
  scaled_to_zero: [service_pods]
  crash_loop: [service_pods, service_errors]
  not_schedulable: [service_pods]
# Not "not ready": Prometheus scrapes a pod whether or not it is ready, so a service that runs but
# fails its readiness is still up for this alert (tried live, 2026-10-23: it never fired). A
# service that cannot reach its database at start-up exits instead: a crash loop.
proposals:
  scaled_to_zero: >
    Scale {job} back up (kubectl -n payflow scale deployment {job} --replicas=2, or what its manifest
    says) and find out what scaled it down.
  crash_loop: >
    Read why {job}'s container died (its last exit, and kubectl -n payflow logs deploy/{job} --previous):
    restore the dependency it cannot reach, or fix the setting, secret or memory limit it dies of, and
    roll it out again.
  not_schedulable: >
    Make room in the cluster, or lower {job}'s resource requests: kubectl -n payflow describe pod
    shows what the scheduler is missing.
checks:
  service_pods:
    tool: get_pod_status
    arguments: {workload: "{job}"}
    tells: Whether {job}'s pods are wanted, running and ready, or crashing, pending or not ready, with their last exit and Kubernetes' warning events.
  service_errors:
    tool: get_recent_errors
    arguments: {workload: "{job}", minutes: 15}
    tells: What {job} logged, including why it failed to start (a refused database connection, a missing setting) and lines from a container that has since died.
  service_health:
    tool: get_service_health
    arguments: {service: "{job}"}
    tells: How many instances of {job} Prometheus sees up, and how it answers requests.
  database:
    tool: get_pod_status
    arguments: {workload: postgres}
    tells: Whether the database every service uses is running and ready (a service that cannot reach it at start-up dies).
  kafka:
    tool: get_pod_status
    arguments: {workload: kafka}
    tells: Whether Kafka is running and ready.
---
# ServiceDown

## What it means

Prometheus sees no healthy instance of a PayFlow service, the one in the alert's `job` label, for
two minutes: every instance it scrapes is down, or there is none to scrape at all. Requests to
that service fail (the gateway answers 502 or 503 for it), and whatever depends on it stalls.

## Telling the causes apart

The service's pods say most of it:

- **Scaled to zero**: no pods are wanted. Nothing crashed; someone or something set its replicas
  to zero.
- **Crash loop**: pods in `Waiting: CrashLoopBackOff`, or restarted in the last minutes, with a
  last exit such as `Error (exit 1)` or `OOMKilled (exit 137)`. The service's log (lines from the
  container that died) says why.
- **Not schedulable**: pods `Pending: cannot be scheduled`, with Kubernetes' reason (insufficient
  memory or CPU).
- A crash at start-up is often a dependency: the service's log says it could not reach the
  database or Kafka, and their pods say whether they are running.

A pod that runs but is never ready does not raise this alert: Prometheus still scrapes it.

## What resolves it

Restoring the pods: scaling the service back up, fixing what kills its container (or restoring
the dependency it dies without), or making room for it. Nothing in PayFlow needs to be replayed
afterwards: messages wait in Kafka and the outboxes, and sagas resume their steps. The alert
resolves two minutes after an instance is healthy again.
