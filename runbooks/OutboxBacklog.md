---
alert: OutboxBacklog
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", OutboxBacklog / OutboxMessageParked; the alert rule (oldest pending over 2 minutes, or over 1000 pending, for 2 minutes)
causes:
  kafka_unreachable: Kafka is down or failing, so the service cannot publish what its outbox holds.
  relay_failing: Kafka is fine, but this service's outbox relay keeps failing to publish (its log names the error).
  burst: Kafka and the relay work and the backlog is draining; more was written at once than is published in two minutes.
# {application} is the service whose outbox is not draining.
shown_by:
  kafka_unreachable: [kafka_pods, kafka_errors, messaging]
  relay_failing: [service_errors]
  burst: [messaging]
requires_clear:
  # the relay or a burst is the cause only while Kafka is fine
  relay_failing: [kafka_pods]
  burst: [kafka_pods, service_errors]
proposals:
  kafka_unreachable: >
    Bring Kafka back (see KafkaUnreachable). Nothing is lost: the messages wait in {application}'s
    outbox table and are published in order once Kafka answers.
  relay_failing: >
    Fix what {application}'s relay fails on (its log says); a message that can never be published
    is parked (OutboxMessageParked) and blocks the later messages of its payment.
  burst: >
    Nothing to do while the backlog falls; if it keeps growing, look at {application}'s relay
    throughput and Kafka's health.
checks:
  messaging:
    tool: get_messaging_status
    arguments: {minutes: 15}
    tells: Each service's outbox (pending, change over 15 minutes, the oldest's age, parked), whether Kafka is reachable, and the consumers' lag.
  kafka_pods:
    tool: get_pod_status
    arguments: {workload: kafka}
    tells: Whether Kafka is running and ready.
  kafka_errors:
    tool: get_recent_errors
    arguments: {workload: kafka, minutes: 15}
    tells: What the broker logged at WARN or ERROR.
  service_errors:
    tool: get_recent_errors
    arguments: {workload: "{application}", minutes: 15}
    tells: What {application} logged, including its outbox relay failing to publish and its Kafka client failing to connect.
---
# OutboxBacklog

## What it means

Every PayFlow service writes the messages it sends (events, saga commands, webhooks) to its own
outbox table, in the same transaction as the change they describe, and a relay publishes them to
Kafka. The alert fires when a service's (the `application` label) oldest unpublished message is
over two minutes old, or over 1000 wait, for two minutes. Nothing is lost; everything those
messages drive is late.

## Telling the causes apart

- **Kafka unreachable**: Kafka's pod is down or failing; every service's outbox piles up, and their
  Kafka clients log that they cannot connect.
- **Relay failing**: Kafka is fine and other outboxes drain, but this service's relay logs the same
  publish error again and again.
- **A burst**: Kafka is fine and the backlog is falling.

## What resolves it

Kafka (or the relay) publishing again: the outbox drains in order by itself. The alert resolves
two minutes after the oldest message is under two minutes old.
