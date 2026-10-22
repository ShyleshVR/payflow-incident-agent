---
alert: SagaRequiresAttention
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", SagaRequiresAttention; the parking codes in payment-service's PaymentSagaStateMachine and payout-service's PayoutSagaStateMachine
causes:
  processor_no_answer: A payment or refund saga parked because the card processor gave no answer in time to a capture, a void or a refund, so whether the money moved is unknown.
  bank_no_answer: A payout saga parked because the bank gave no answer in time to a transfer or a status request, or a transfer stayed in transit too long.
  partner_rejected: The processor or the bank answered, and refused the request (or reported a transfer state that makes no sense). Sending it again would be refused again.
  ledger_rejected: The ledger refused a posting the saga asked for (a settlement, a hold, a release, a refund or payout posting, a return).
# Code, not the model, sorts each parked saga into one of the causes by the code PayFlow parked it
# for (the outcome of its last step), and only sagas of a retryable cause go on the retry list:
# no answer came, so a retry asks again under the same idempotency key. A code listed nowhere
# needs a person. Taken from the two state machines; a new code there must be added here.
parking_codes:
  processor_no_answer: [CAPTURE_UNKNOWN, VOID_UNKNOWN, REFUND_UNKNOWN]
  bank_no_answer: [TRANSFER_UNKNOWN, TRANSFER_STATUS_UNKNOWN, IN_TRANSIT_TOO_LONG]
  partner_rejected: [CAPTURE_REJECTED, VOID_REJECTED, TRANSFER_REJECTED, TRANSFER_STATUS_REJECTED, UNEXPECTED_TRANSFER_STATE]
  ledger_rejected: [SETTLEMENT_REJECTED, HOLD_REJECTED, RELEASE_REJECTED, FINALIZE_REJECTED, RETURN_REJECTED]
retryable: [processor_no_answer, bank_no_answer]
# A diagnosis must quote one of these checks, from one that found something. The parked sagas'
# own last errors name the reason; the services' logs repeat it when the saga parked.
shown_by:
  processor_no_answer: [parked_sagas, payment_service_errors, partner_calls]
  bank_no_answer: [parked_sagas, payout_service_errors, partner_calls]
  partner_rejected: [parked_sagas, payment_service_errors, payout_service_errors]
  ledger_rejected: [parked_sagas, ledger_errors, payment_service_errors, payout_service_errors]
# The next step for each cause. For the two retryable causes it comes with the retry list code
# made; for the others a person acts, and approving only records the decision.
proposals:
  processor_no_answer: >
    Retry the sagas listed below once the processor answers again. Each resumes the step it
    parked in under the same idempotency key, so a capture or refund that did go through is not
    made twice. Just before it is retried, each is checked again (still parked, the processor
    answering); any that is not is skipped and reported.
  bank_no_answer: >
    Retry the sagas listed below once the bank answers again. Each resumes the step it parked in
    under the same idempotency key, so a transfer is not sent twice. Just before it is retried,
    each is checked again (still parked, the bank answering); any that is not is skipped and
    reported.
  partner_rejected: >
    Look at each saga that needs a person: the processor or the bank refused the request, and the
    same request would be refused again. Find out why with the partner (the last error has their
    answer), correct what they object to, and only then retry the saga by hand.
  ledger_rejected: >
    Look at each saga that needs a person: the ledger refused a posting. Compare the ledger's
    postings for that payment or payout with what the saga asked for (the ledger's log names the
    reason), correct the cause, and only then retry the saga by hand.
checks:
  parked_sagas:
    tool: list_sagas_needing_attention
    arguments: {}
    tells: The parked sagas (and stuck ones) of payments, refunds and payouts, with the step each stopped in, for how long, and its last error, which says why it parked.
  partner_calls:
    tool: get_dependency_status
    arguments: {minutes: 15}
    tells: How calls to the card processor (from payment-service) and to the bank (from payout-service) ended in the last 15 minutes, answered or not, and the processor's circuit breaker state. Shows whether the partner answers now.
  payment_service_errors:
    tool: get_recent_errors
    arguments: {workload: payment-service, minutes: 15}
    tells: What payment-service logged, including each saga it parked and why, and calls to the processor that failed.
  payout_service_errors:
    tool: get_recent_errors
    arguments: {workload: payout-service, minutes: 15}
    tells: What payout-service logged, including each payout saga it parked and why, and calls to the bank that failed.
  ledger_errors:
    tool: get_recent_errors
    arguments: {workload: ledger-service, minutes: 15}
    tells: What the ledger logged, including postings it refused and why.
---
# SagaRequiresAttention

## What it means

A saga is PayFlow's step-by-step record of one money movement: a payment (authorize, capture,
settle), a refund, or a payout to a merchant's bank account. When a step reaches an outcome that
must not be guessed, the saga stops there and waits for an operator: it is **parked**. The alert
fires when any saga of a service (payment-service or payout-service, in its `application` label)
has been parked for a minute.

A parked saga does nothing more by itself. It resumes only when someone retries it, and a retry
runs the step it parked in again, under the same idempotency key.

## Telling the causes apart

Each saga's last error says why it parked:

- **No answer from the processor** ("No capture answer within ...", "Authorization not released
  within ...", "No refund answer within ..."): the processor did not answer a capture, a void or a
  refund for the whole step timeout (10 minutes for a capture). Whether money moved is unknown.
  This usually follows an outage of the processor; `ProcessorCircuitOpen` may be firing or may
  have been.
- **No answer from the bank** ("No answer to the transfer within ...", "No answer about the
  transfer within ...", "... still in transit after ..."): the same for a payout.
- **A partner said no** ("Processor rejected the capture: ...", "Processor refused to release the
  authorization: ...", "Bank rejected the transfer request: ...", "Bank refused the status
  request: ...", "Bank reported ..."): an answer came, and it was a refusal.
- **The ledger said no** ("Ledger rejected the settlement: ...", "... the hold: ...", "... the
  hold release: ...", "... the refund posting: ...", "... the payout posting: ...", "... the return
  posting: ..."): the ledger refused a posting.

Several sagas can be parked at once, for different reasons. Name the cause behind most of them,
and say if others parked for another reason. Whether the processor or the bank answers **now**
matters for what happens next: say what the partner calls show.

## What the agent does with them

The agent's code reads every parked saga of the service and sorts it by the code it parked for.
Only sagas parked because no answer came are put on the list to retry, at most 50; the others
need a person. The diagnosis explains the incident; it does not choose which sagas are retried.

## What resolves it

Retrying each parked saga once its cause is gone: `POST /api/v1/payments/{id}/saga/retry` (or
`/api/v1/payouts/{id}/saga/retry`) resumes the step under the same idempotency key, so nothing
is charged, refunded or paid out twice. For a saga that parked without an answer, the cause is
gone when the partner answers again. For one that a partner or the ledger refused, a person must
find and correct why first; retrying it unchanged is refused again. The alert resolves when no
saga of the service is parked.
