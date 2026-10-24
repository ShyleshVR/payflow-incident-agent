---
alert: OutboxMessageParked
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", OutboxBacklog / OutboxMessageParked; the alert rule (outbox_failed above 0 for a minute); payment-service's OutboxPublisher and the ledger's and payout-service's OutboxRelay, which park a message whose error retrying cannot fix
causes:
  too_large: The message is larger than Kafka accepts (RecordTooLargeException), so it can never be published as it is.
  not_serializable: The message's content cannot be made into a Kafka record (its payload is not valid JSON, a Jackson error such as JsonParseException; or a SerializationException), so it can never be published as it is.
# {application} is the service whose outbox holds the parked message.
shown_by:
  too_large: [service_errors]
  not_serializable: [service_errors]
proposals:
  too_large: >
    Find the parked message in {application}'s outbox (SELECT * FROM outbox_event WHERE status =
    'FAILED') and find what made it so large; fix or discard it, and the later messages of its
    payment resume. Nothing is published until then.
  not_serializable: >
    Find the parked message in {application}'s outbox (SELECT * FROM outbox_event WHERE status =
    'FAILED') and what in it cannot be encoded; fix or discard it, and the later messages of its
    payment resume. If the code wrote it, the same will happen again until the code is fixed.
checks:
  messaging:
    tool: get_messaging_status
    arguments: {minutes: 15}
    tells: Each service's outbox (pending, change over 15 minutes, the oldest's age, parked), and whether Kafka is reachable.
  service_errors:
    tool: get_recent_errors
    arguments: {workload: "{application}", minutes: 15}
    tells: What {application} logged, including the line its outbox writes when it parks a message, with the error that made it give up.
  kafka_pods:
    tool: get_pod_status
    arguments: {workload: kafka}
    tells: Whether Kafka is running and ready.
---
# OutboxMessageParked

## What it means

A service's outbox relay (the `application` label) met a message that retrying cannot publish: an
error that says the message itself is wrong, not that Kafka is away. It set the message aside as
FAILED instead of retrying it forever. The later messages of the same payment are held behind it,
so that a payment's events stay in order; other payments' messages are published as usual. The
alert fires a minute after the first parked message.

## Telling the causes apart

The service logs one ERROR line when it parks a message ("can never be published and was parked
as FAILED"), ending with the error that made it give up (the innermost one, by its class's short name):

- **Too large**: `RecordTooLargeException` (the message, in bytes, against Kafka's limit).
- **Not serializable**: the payload is not valid JSON (Jackson's `JsonParseException` or another
  `JsonProcessingException`), or a `SerializationException`.

Kafka being down never parks a message: those errors are retried, and show as OutboxBacklog.

## What resolves it

Someone fixing or discarding the parked message; the alert resolves a minute after no message is
FAILED. Parking is permanent by design: nothing retries it by itself.
