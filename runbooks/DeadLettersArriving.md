---
alert: DeadLettersArriving
based_on: PayFlow docs/DEPLOYMENT.md (DeadLettersArriving); the consumers' dead-letter handling (a message that fails its retries goes to <topic>.DLT)
causes:
  unreadable_message: A message on the topic cannot be read at all (it is not the JSON its consumers expect), so every consumer of it gives up at once.
  processing_failure: A readable message fails every time it is handled (a missing record, a value the code rejects), so its consumer gives up after its retries.
# {topic} is the dead-letter topic (the original topic with .DLT).
shown_by:
  unreadable_message: [notification_errors, webhook_errors, ledger_errors, payment_service_errors, payout_service_errors]
  processing_failure: [notification_errors, webhook_errors, ledger_errors, payment_service_errors, payout_service_errors]
proposals:
  unreadable_message: >
    Find who wrote the unreadable message to the original topic (only PayFlow's own services should);
    the dead letter keeps it for inspection. Nothing is retried by itself; later messages are not held up.
  processing_failure: >
    Read the consumer's error for that message and fix the record or the code it fails on; then
    replay the dead letter onto the original topic if the work still needs doing.
checks:
  messaging:
    tool: get_messaging_status
    arguments: {minutes: 15}
    tells: Which dead-letter topics received messages in the last 15 minutes and how many, and the consumers' lag.
  notification_errors:
    tool: get_recent_errors
    arguments: {workload: notification-service, minutes: 15}
    tells: What notification-service (a consumer of payment-created) logged, including messages it could not read or handle.
  webhook_errors:
    tool: get_recent_errors
    arguments: {workload: webhook-service, minutes: 15}
    tells: What webhook-service (a consumer of payment-created and payout-events) logged, including messages it could not read or handle.
  ledger_errors:
    tool: get_recent_errors
    arguments: {workload: ledger-service, minutes: 15}
    tells: What the ledger (the consumer of ledger-commands) logged.
  payment_service_errors:
    tool: get_recent_errors
    arguments: {workload: payment-service, minutes: 15}
    tells: What payment-service (the consumer of ledger-replies) logged.
  payout_service_errors:
    tool: get_recent_errors
    arguments: {workload: payout-service, minutes: 15}
    tells: What payout-service (the consumer of payout-ledger-replies) logged.
---
# DeadLettersArriving

## What it means

A consumer that cannot handle a message, after its retries, sends it to the topic's dead-letter
topic (`<topic>.DLT`, the alert's `topic` label) and moves on. The alert fires when a dead-letter
topic received a message in the last ten minutes. The message is kept there; what it should have
caused (a notification, a webhook, a ledger posting) did not happen.

## Telling the causes apart

The consumers' logs say which message failed and why:

- **Unreadable**: a deserialization error (the payload is not JSON, or not the expected shape); the
  message goes to the dead letter at once, without retries, and every consumer of the topic does
  the same.
- **Processing failure**: the message was read, but handling it threw the same error on every
  retry (a payment the consumer cannot find, a value it rejects).

## What resolves it

Nothing resolves it by itself but time: the alert clears ten minutes after the last dead letter.
The work is only done once the message is understood and, if still needed, replayed.
