---
alert: PayoutsFailing
based_on: PayFlow docs/PAYOUTS.md, section "Runbook", PayoutsFailing; the failure codes payout-service records
causes:
  merchant_details_wrong: The bank declined the transfers because the merchants' bank accounts are wrong or closed (invalid_account, account_closed). A problem with the merchants' details, not with PayFlow or the bank.
  returned_by_bank: Payouts that were paid came back from the bank (a return, such as account_frozen), days after they left.
  balance_short: PayFlow's ledger held less than the payout asked for (insufficient_payable_balance), so the payout's amount and the merchant's payable balance disagree.
shown_by:
  merchant_details_wrong: [failed_payouts, bank_calls]
  returned_by_bank: [failed_payouts]
  balance_short: [failed_payouts, ledger_errors]
proposals:
  merchant_details_wrong: >
    Contact the merchants of the payouts listed (their destination's bank account is wrong or
    closed) and have them set a correct one (PUT /api/v1/payouts/destination). Their money is back
    in their balance; the next batch pays it.
  returned_by_bank: >
    Ask the bank why the transfers came back (the failure code is its reason), and the merchants to
    check their account. The money is booked back to their balance; the next batch pays it again.
  balance_short: >
    Compare the merchant's payable balance in the ledger with what was asked for: refunds taken
    from the same balance, or an instant payout racing the batch. Nothing left the merchant's balance.
checks:
  failed_payouts:
    tool: get_failed_payouts
    arguments: {hours: 1}
    tells: The payouts that failed or came back in the last hour, grouped by failure code (the bank's or the ledger's answer), with how many were paid meanwhile.
  bank_calls:
    tool: get_dependency_status
    arguments: {minutes: 60}
    tells: How payout-service's calls to the bank ended in the last hour; a decline is the bank answering that an account is not accepted.
  payout_service_errors:
    tool: get_recent_errors
    arguments: {workload: payout-service, minutes: 60}
    tells: What payout-service logged in the last hour, including the ledger refusing a payout's hold.
  ledger_errors:
    tool: get_recent_errors
    arguments: {workload: ledger-service, minutes: 60}
    tells: What the ledger logged in the last hour, including each payout hold it refused and why ("Ledger command refused ... HOLD_PAYOUT ... reason=INSUFFICIENT_FUNDS").
  parked_payouts:
    tool: list_sagas_needing_attention
    arguments: {}
    tells: Payouts parked for an operator (a transfer the bank never answered); a bank outage shows there, not as failed payouts.
---
# PayoutsFailing

## What it means

A payout sends money from a merchant's balance in PayFlow to the merchant's bank account. The
alert fires when 3 or more payouts failed (never paid) or were returned (paid, then sent back by
the bank) within an hour. The money is never lost: a failed payout releases its hold, and a
returned one is booked back to the merchant's balance.

## Telling the causes apart

The failure code of each payout says why:

- **invalid_account, account_closed** (status FAILED): the bank declined the transfer: the
  merchant's destination account is wrong or closed. Usually a few merchants, every payout of theirs.
- **A return code, such as account_frozen** (status RETURNED): the payout was paid and came back.
  Many at once from different merchants points at the bank.
- **insufficient_payable_balance** (status FAILED): the ledger refused the hold: there was less to
  pay out than asked.

A bank that does not answer at all does not fail payouts: it parks them for an operator, and
`SagaRequiresAttention` fires for payout-service instead.

## What resolves it

Fixing the merchants' destinations (or settling the returns with the bank): the next batch pays
the money still in their balances. The alert resolves an hour after the last failure.
