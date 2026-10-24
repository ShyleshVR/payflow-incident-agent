---
alert: ReconciliationRunFailed
based_on: PayFlow docs/RECONCILIATION.md, sections "Scheduling", "Reading the sources" and "Runbook" (ReconciliationNotRun / ReconciliationRunFailed); the run errors recorded in its database
causes:
  processor_unavailable: The processor's settlement report could not be read (processor-simulator did not answer, was down, or crashed while serving it).
  ledger_unavailable: The ledger's transactions or refund holds could not be read (ledger-service did not answer).
  payments_unavailable: payment-service's payment lookups could not be read.
  payouts_unavailable: payout-service's payout lookups could not be read.
  auth_unavailable: reconciliation-service could not get its token from merchant-service, so it could read nothing.
  run_abandoned: The run stopped reporting because its pod died (killed, out of memory, restarted) and a later attempt took it over.
# Code, not the model, sorts each day no run has reconciled into a cause by the source its last
# error names: Java's text names the URL that did not answer, and a run whose pod died begins
# "Abandoned". A run reads and compares and moves no money, so a rerun is safe; but a day that has
# failed 3 times needs a person (agent/reruns.py): something keeps breaking it.
error_sources:
  processor_unavailable: [processor-simulator]
  ledger_unavailable: [ledger-service]
  payments_unavailable: [payment-service]
  payouts_unavailable: [payout-service]
  auth_unavailable: [merchant-service, /oauth2/token]
  run_abandoned: ["Abandoned"]
rerunnable: [processor_unavailable, ledger_unavailable, payments_unavailable, payouts_unavailable, auth_unavailable, run_abandoned]
# A diagnosis must quote one of these, from one that found something: the failed run's error names
# the source; reconciliation-service's log repeats it; the source's own pods show it down.
shown_by:
  processor_unavailable: [reconciliation, reconciliation_errors, processor_pods]
  ledger_unavailable: [reconciliation, reconciliation_errors, ledger_pods]
  payments_unavailable: [reconciliation, reconciliation_errors, payment_service_pods]
  payouts_unavailable: [reconciliation, reconciliation_errors, payout_service_pods]
  auth_unavailable: [reconciliation, reconciliation_errors, merchant_service_pods]
  run_abandoned: [reconciliation, reconciliation_errors, reconciliation_pods]
proposals:
  processor_unavailable: >
    Run the reconciliation of the days listed below again once the processor answers. Just before
    each, code checks that the day is still not reconciled and processor-simulator is healthy. If
    the processor crashed while serving the report, look at its memory first: a rerun would crash it again.
  ledger_unavailable: >
    Run the reconciliation of the days listed below again once the ledger answers. Just before
    each, code checks that the day is still not reconciled and ledger-service is healthy.
  payments_unavailable: >
    Run the reconciliation of the days listed below again once payment-service answers. Just
    before each, code checks that the day is still not reconciled and payment-service is healthy.
  payouts_unavailable: >
    Run the reconciliation of the days listed below again once payout-service answers. Just
    before each, code checks that the day is still not reconciled and payout-service is healthy.
  auth_unavailable: >
    Run the reconciliation of the days listed below again once merchant-service issues tokens.
    Just before each, code checks that the day is still not reconciled and merchant-service is healthy.
  run_abandoned: >
    Run the reconciliation of the days listed below again. Just before each, code checks that the
    day is still not reconciled and reconciliation-service is healthy. If its pod died of memory,
    look at that first: the same day may kill it again.
checks:
  reconciliation:
    tool: get_reconciliation
    arguments: {}
    tells: The last 7 days at a glance (clean, discrepancies, failed, running, not reconciled), and the newest day that is not clean in detail, with its failed runs and the last one's error, which names the source that did not answer.
  reconciliation_errors:
    tool: get_recent_errors
    arguments: {workload: reconciliation-service, minutes: 60}
    tells: What reconciliation-service logged in the last hour, including each failed run ("Reconciliation of <day> failed") with its cause.
  processor_pods:
    tool: get_pod_status
    arguments: {workload: processor-simulator}
    tells: Whether the processor's pods are running and ready, or were restarted (and why, such as OOMKilled).
  ledger_pods:
    tool: get_pod_status
    arguments: {workload: ledger-service}
    tells: Whether the ledger's pods are running and ready.
  payment_service_pods:
    tool: get_pod_status
    arguments: {workload: payment-service}
    tells: Whether payment-service's pods are running and ready.
  payout_service_pods:
    tool: get_pod_status
    arguments: {workload: payout-service}
    tells: Whether payout-service's pods are running and ready.
  merchant_service_pods:
    tool: get_pod_status
    arguments: {workload: merchant-service}
    tells: Whether merchant-service, which issues every service's tokens, is running and ready.
  reconciliation_pods:
    tool: get_pod_status
    arguments: {workload: reconciliation-service}
    tells: Whether reconciliation-service's own pods are running, and whether one was restarted or killed (and why).
---
# ReconciliationRunFailed

## What it means

Every day, PayFlow reconciles the previous business day: it reads the processor's settlement
report, the ledger's transactions and refund holds, and payment-service's and payout-service's
statuses, and compares them. The alert fires when a run failed in the last hour. A run fails as a
whole when a source cannot be read; it never records a partial result, which would show up as
false discrepancies.

The alert keeps firing for an hour after the failure, even when a later run reconciled the day:
the day's status in the reconciliation check says whether it still needs one.

## Telling the causes apart

The failed run's error names what did not answer, as a URL:

- **processor-simulator** (`.../v1/reports/settlement/...`): the processor's report. If its pods
  were restarted and their last exit was OOMKilled, the report itself crashed it.
- **ledger-service** (`.../api/v1/ledger/...`): the ledger.
- **payment-service** (`.../api/v1/payments/lookup`) or **payout-service** (`.../api/v1/payouts/lookup`).
- **merchant-service** (`.../oauth2/token`): no token, so nothing could be read.
- **"Abandoned: the run stopped reporting"**: the run's own pod died mid-run, and a later attempt
  took the day over. reconciliation-service's pods show the restart.

The source's own pods say whether it is back now.

## What the agent does with them

The agent's code reads every day of the last week that no run has reconciled and whose newest run
failed, and sorts each by the source its error names. Days of a known source that have failed
fewer than 3 times are put on the list to run again; the others need a person. The diagnosis
explains the incident; it does not choose the days.

## What resolves it

Bring the source back; then the day is reconciled by the hourly catch-up (for the last 3 days),
or at once by a manual run: `POST /api/v1/reconciliation/runs {"date": "..."}`. A run reads and
compares, so running a day again is safe. A day that keeps failing has something that breaks it
every time (on 2026-10-11 the processor ran out of memory serving a 2.75-million-payment day,
every hour); running it again only repeats that. The alert resolves an hour after the last
failure.
