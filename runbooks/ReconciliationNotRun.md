---
alert: ReconciliationNotRun
based_on: PayFlow docs/RECONCILIATION.md, sections "Scheduling" and "Runbook" (ReconciliationNotRun / ReconciliationRunFailed)
causes:
  service_not_running: reconciliation-service has no running, ready pod, so nothing schedules a run.
  runs_failing: Runs start but every one fails, because a source cannot be read (ReconciliationRunFailed fires too).
  runs_not_finishing: A run is still going, or keeps being taken over (a very large day, or pods killed in the middle), so none completes.
shown_by:
  service_not_running: [reconciliation_pods]
  runs_failing: [reconciliation, reconciliation_errors]
  runs_not_finishing: [reconciliation, reconciliation_errors, reconciliation_pods]
requires_clear:
  runs_failing: [reconciliation_pods]
proposals:
  service_not_running: >
    Bring reconciliation-service back; a minute after it starts, the catch-up reconciles every one of
    the last 3 days that has no completed run.
  runs_failing: >
    Bring back the source the failed runs name (see ReconciliationRunFailed); the hourly catch-up
    then reconciles the days, or run them by hand.
  runs_not_finishing: >
    Look at the run in progress (how long, which day) and at reconciliation-service's pods (memory,
    restarts); a day too large for the pod needs it fixed before the run can finish.
checks:
  reconciliation:
    tool: get_reconciliation
    arguments: {}
    tells: The last 7 days at a glance (clean, failed, running, not reconciled), how long since a run completed, and the newest day that is not clean in detail, with a failed run's error or a run in progress.
  reconciliation_pods:
    tool: get_pod_status
    arguments: {workload: reconciliation-service}
    tells: Whether reconciliation-service's pods are running and ready, and whether they were restarted or killed (and why).
  reconciliation_errors:
    tool: get_recent_errors
    arguments: {workload: reconciliation-service, minutes: 60}
    tells: What reconciliation-service logged in the last hour, including failed runs and runs taken over.
---
# ReconciliationNotRun

## What it means

No reconciliation run has completed for over 26 hours, for half an hour. The nightly run (02:00
UTC) and the hourly catch-up both failed to reconcile anything in that time. Discrepancies, if
there are any, are not being found.

## Telling the causes apart

- **Not running**: reconciliation-service has no ready pod; no run started at all.
- **Runs failing**: runs start and fail, each with the error of the source it could not read.
- **Runs not finishing**: a run is in progress, or runs keep being abandoned and taken over;
  reconciliation-service's pods may show restarts (OOMKilled).

## What resolves it

A completed run of any day: the catch-up reconciles the missing days once the service and its
sources work. The alert resolves when a run completes.
