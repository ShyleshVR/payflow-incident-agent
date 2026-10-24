---
alert: InstanceDown
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", ServiceDown / InstanceDown; the alert rule (one scrape target down for 2 minutes)
causes:
  instance_crashing: One pod of the service keeps dying and restarting (its last exit, its restarts, back-off), while the others run.
  instance_hanging: One pod runs but does not answer (its process is stuck, starved or swapping), so Prometheus cannot scrape it; Kubernetes may restart it when its liveness probe fails.
# {job} is the service, {pod} the pod the alert is about.
shown_by:
  instance_crashing: [service_pods, service_errors]
  instance_hanging: [service_pods, service_errors, service_health]
proposals:
  instance_crashing: >
    Read why {pod} dies (its last exit; kubectl -n payflow logs {pod} --previous). If it is only
    this pod, delete it and let the deployment make a new one; if the new one dies too, treat it as
    the service's problem (ServiceDown's runbook).
  instance_hanging: >
    Look at {pod} (kubectl -n payflow describe pod {pod}, its log, its memory); if it does not
    recover, delete it and let the deployment make a new one.
checks:
  service_pods:
    tool: get_pod_status
    arguments: {workload: "{job}"}
    tells: "{job}'s pods: which are unhealthy ({pod} among them?), their restarts, last exit and warning events."
  service_errors:
    tool: get_recent_errors
    arguments: {workload: "{job}", minutes: 15}
    tells: What {job}'s pods logged, including lines from a container that has since died.
  service_health:
    tool: get_service_health
    arguments: {service: "{job}"}
    tells: How many of {job}'s instances Prometheus sees up, and how the service answers.
---
# InstanceDown

## What it means

One pod of a service (the alert's `pod` label; the service is `job`) has not been scraped by
Prometheus for two minutes, while the service as a whole may still be up (if every pod is down,
`ServiceDown` fires too). The service runs with less room; requests go to its other pods.

## Telling the causes apart

- **Crashing**: that pod's restart count grows and its last exit says why (Error, OOMKilled); its
  log, from the container that died, often says more.
- **Hanging**: the pod is Running and its restart count stays, but it does not answer; its health
  shows fewer instances up than wanted.

A pod being created or rescheduled has no address to scrape yet, so it does not fire this alert.

## What resolves it

The pod answering again: restarted by Kubernetes or replaced by deleting it. The alert resolves two
minutes after it is scraped again.
