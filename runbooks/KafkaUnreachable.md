---
alert: KafkaUnreachable
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", KafkaUnreachable; the alert rule (kafka-exporter's own up metric)
causes:
  kafka_not_running: Kafka's pod (kafka-0) is missing, crashing, pending or not ready, so nothing can reach the broker.
  kafka_unhealthy: Kafka's pod runs and is ready, but the broker itself is failing (its own log shows errors), so clients cannot use it.
  exporter_down: kafka-exporter, which the alert watches, is not running; Kafka itself is fine and PayFlow's messages flow. Only the alerting is blind.
# The alert means "kafka-exporter cannot be scraped or cannot read the broker": it cannot tell a
# broken broker from a broken exporter. The messaging check tells them apart: with Kafka really
# down the outboxes stop draining.
shown_by:
  kafka_not_running: [kafka_pods]
  kafka_unhealthy: [kafka_errors, messaging]
  exporter_down: [exporter_pods]
requires_clear:
  # a failing broker is a cause only while its pod is fine; the exporter only while Kafka is fine
  kafka_unhealthy: [kafka_pods]
  exporter_down: [kafka_pods, kafka_errors]
proposals:
  kafka_not_running: >
    Bring Kafka back: kubectl -n payflow get pod kafka-0, then describe and logs; scale the
    statefulset back to 1 if it was scaled down. Nothing is lost meanwhile: the services keep their
    messages in their outboxes, and they drain once the broker answers.
  kafka_unhealthy: >
    Read Kafka's own log for the failure (storage, memory, the controller) and fix it, restarting
    kafka-0 if needed. The outboxes drain once it answers again.
  exporter_down: >
    Bring kafka-exporter back (kubectl -n payflow get pods -l app=kafka-exporter, then describe
    and logs). PayFlow is not affected; until then Kafka's lag and reachability are not watched.
checks:
  kafka_pods:
    tool: get_pod_status
    arguments: {workload: kafka}
    tells: Whether Kafka's pod is wanted, running and ready, or crashing, pending or missing, with its last exit and warning events.
  kafka_errors:
    tool: get_recent_errors
    arguments: {workload: kafka, minutes: 15}
    tells: What the broker itself logged at WARN or ERROR (storage, memory, the controller, clients it could not serve).
  exporter_pods:
    tool: get_pod_status
    arguments: {workload: kafka-exporter}
    tells: Whether kafka-exporter, the program the alert watches, is running and ready.
  messaging:
    tool: get_messaging_status
    arguments: {minutes: 15}
    tells: Whether Kafka is reachable from Prometheus' side, the consumer groups' lag, and whether the services' outboxes are draining or piling up (they pile up when the broker is really unreachable).
  payment_service_errors:
    tool: get_recent_errors
    arguments: {workload: payment-service, minutes: 15}
    tells: What payment-service logged, including failures to publish its outbox to Kafka.
---
# KafkaUnreachable

## What it means

PayFlow's services talk through Kafka: payment events, ledger commands and replies, webhooks. The
alert fires when kafka-exporter, which reads the broker for Prometheus, has been down for two
minutes. That has two very different meanings: **the broker cannot be reached**, or **the
exporter itself is not running** while Kafka is fine.

## Telling the causes apart

- **Kafka not running**: kafka-0 is missing (scaled to zero), crash-looping, pending or not ready.
  The services' outboxes pile up: they cannot publish.
- **Kafka unhealthy**: kafka-0 runs and is ready, but its own log shows the broker failing, and the
  outboxes pile up all the same.
- **Exporter down**: kafka-exporter's pod is missing or crashing, while kafka-0 is fine and the
  outboxes drain as usual. Only the watching is broken.

## What happens to PayFlow meanwhile

Nothing is lost. Every service writes its messages to its own outbox table in the same
transaction as the change they describe, and publishes them when Kafka answers; expect
`OutboxBacklog` after a couple of minutes, and `SagaStepStuck` if it lasts, since saga steps wait
for the ledger's replies. Payments keep being accepted.

## What resolves it

Kafka (or the exporter) back: the outboxes drain by themselves, the consumers catch up, and the
alert resolves two minutes after the exporter can read the broker again.
