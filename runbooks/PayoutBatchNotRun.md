---
alert: PayoutBatchNotRun
based_on: PayFlow docs/PAYOUTS.md, section "Runbook" (PayoutBatchNotRun) and "Batch and instant payouts"
causes:
  service_not_running: payout-service has no running, ready pod, so the daily batch is not scheduled.
  batch_failing: The batch starts but fails, usually because the ledger cannot be read for the payable balances.
shown_by:
  service_not_running: [payout_pods]
  batch_failing: [payout_errors, ledger_pods]
requires_clear:
  batch_failing: [payout_pods]
proposals:
  service_not_running: >
    Bring payout-service back; the hourly catch-up runs the missed batch after its time (04:00 UTC).
  batch_failing: >
    Bring back what the batch could not read (usually the ledger, for the payable balances); the
    hourly catch-up then runs it, or run it by hand (POST /api/v1/payouts/batches).
checks:
  payout_pods:
    tool: get_pod_status
    arguments: {workload: payout-service}
    tells: Whether payout-service's pods are running and ready.
  payout_errors:
    tool: get_recent_errors
    arguments: {workload: payout-service, minutes: 60}
    tells: What payout-service logged in the last hour, including a batch that failed and why.
  ledger_pods:
    tool: get_pod_status
    arguments: {workload: ledger-service}
    tells: Whether the ledger, which the batch reads the payable balances from, is running and ready.
  ledger_errors:
    tool: get_recent_errors
    arguments: {workload: ledger-service, minutes: 60}
    tells: What the ledger logged in the last hour.
---
# PayoutBatchNotRun

## What it means

No payout batch has completed for over 26 hours, for half an hour: merchants were not paid their
payable balances yesterday. The batch runs daily at 04:00 UTC, and an hourly catch-up runs it after
that time if it was missed.

## Telling the causes apart

- **Not running**: payout-service has no ready pod.
- **Batch failing**: payout-service runs, and its log shows the batch failing (the ledger's payable
  balances could not be read).

## What resolves it

A completed batch: the catch-up runs it once payout-service and the ledger work, or an operator runs
it (`POST /api/v1/payouts/batches`). Merchants are paid what is payable then; nothing is lost.
