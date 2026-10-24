---
alert: ReconciliationDiscrepancies
based_on: PayFlow docs/RECONCILIATION.md, sections "Discrepancy types" and "Runbook" (ReconciliationDiscrepancies)
causes:
  money_not_booked: Money moved at the processor or the bank (a capture, a refund, a payout paid) that the ledger has no posting for (CAPTURED_NOT_SETTLED, REFUNDED_NOT_BOOKED, PAID_NOT_BOOKED).
  booked_without_money: The ledger has a posting the processor or the bank has nothing for, or has it twice, or for a payment PayFlow does not know (SETTLED_NOT_CAPTURED, BOOKED_NOT_REFUNDED, BOOKED_NOT_PAID, DUPLICATE, UNKNOWN_PAYMENT).
  amounts_differ: Both sides have the money, in different amounts or currencies (SETTLEMENT_AMOUNT_MISMATCH, REFUND_AMOUNT_MISMATCH).
  status_wrong: A payment's status contradicts its money, such as FAILED although captured (STATUS_MISMATCH).
  funds_held: Money is still held that should have been released (AUTHORIZATION_NOT_RELEASED, REFUND_HOLD_STALE).
# {type} is the discrepancy type the alert is about.
shown_by:
  money_not_booked: [reconciliation]
  booked_without_money: [reconciliation]
  amounts_differ: [reconciliation]
  status_wrong: [reconciliation]
  funds_held: [reconciliation]
proposals:
  money_not_booked: >
    For each payment listed, look at its saga and its ledger postings: if the saga is still running
    or parked, resume it; if it finished without the posting, post it. Then run the day's
    reconciliation again.
  booked_without_money: >
    For each one listed, find where the ledger's posting came from (its transaction and command) and
    reverse what has nothing behind it; then run the day's reconciliation again.
  amounts_differ: >
    For each one listed, find which side has the amount the merchant was charged or refunded (the
    processor's record is what moved money) and correct the other; then run the day again.
  status_wrong: >
    Correct the payment's status through its saga (never by hand in the database), then run the day again.
  funds_held: >
    Release what is still held: void the authorization at the processor, or let the refund saga
    finish or release its hold. Then run the day again.
checks:
  reconciliation:
    tool: get_reconciliation
    arguments: {}
    tells: The last 7 days at a glance, and the newest day that is not clean in detail, with its discrepancies by type and up to 3 examples each (the payment or payout, both amounts, its status, what is wrong).
  ledger_pods:
    tool: get_pod_status
    arguments: {workload: ledger-service}
    tells: Whether the ledger is running (a ledger that was down when money moved can miss postings, until the sagas re-send them).
  ledger_errors:
    tool: get_recent_errors
    arguments: {workload: ledger-service, minutes: 60}
    tells: What the ledger logged in the last hour, including postings it refused.
  payment_service_errors:
    tool: get_recent_errors
    arguments: {workload: payment-service, minutes: 60}
    tells: What payment-service logged in the last hour, including sagas it parked.
  parked_sagas:
    tool: list_sagas_needing_attention
    arguments: {}
    tells: Sagas parked or stuck now; a payment whose saga has not finished can explain a posting that is not there yet.
---
# ReconciliationDiscrepancies

## What it means

The daily reconciliation compares, for each payment and payout of a day, what the processor and the
bank say happened, what the ledger booked, and the status the merchant was told. Each disagreement
is a discrepancy. The alert fires for each discrepancy type (the `type` label) found in a recent
day's latest run. Money may be wrong in the ledger, or held when it should not be.

## Telling the causes apart

The type says which; the examples in the reconciliation check say for which payments, with both
amounts and the payment's status:

- **CAPTURED_NOT_SETTLED, REFUNDED_NOT_BOOKED, PAID_NOT_BOOKED**: money moved, the ledger lacks it.
- **SETTLED_NOT_CAPTURED, BOOKED_NOT_REFUNDED, BOOKED_NOT_PAID, DUPLICATE, UNKNOWN_PAYMENT**: the
  ledger has something with nothing behind it.
- **SETTLEMENT_AMOUNT_MISMATCH, REFUND_AMOUNT_MISMATCH**: amounts differ.
- **STATUS_MISMATCH**: the status is wrong for the money.
- **AUTHORIZATION_NOT_RELEASED, REFUND_HOLD_STALE**: money still held.

A payment whose saga is still running is not counted (it is pending), so a discrepancy is one that
nothing in PayFlow will fix by itself.

## What resolves it

Correcting each payment through PayFlow's own flows, then running the day's reconciliation again
(`POST /api/v1/reconciliation/runs`): the alert follows each day's latest run, and resolves when the
re-run is clean.
