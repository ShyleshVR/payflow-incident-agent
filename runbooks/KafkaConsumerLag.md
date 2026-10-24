---
alert: KafkaConsumerLag
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", KafkaConsumerLag; the alert rule (lag over 500 for 5 minutes)
causes:
  consumer_not_running: The consuming service has no running, ready pod, so nobody reads the topic.
  consumer_stalled: The consuming service runs, but does not get through its messages; its log shows the error that holds them (often its database, which by design holds the partition until it recovers).
  traffic_burst: The service consumes normally and the lag is falling; more arrived than it could take at once.
  abandoned_offsets: Nobody consumes the topic for this group at all although the service is healthy; the group stopped reading it (a release) and left its committed offsets behind, so the lag grows for ever.
# {consumergroup} is the consumer group the alert is about; PayFlow names each after its service.
shown_by:
  consumer_not_running: [consumer_pods]
  consumer_stalled: [consumer_errors, messaging, database]
  traffic_burst: [messaging]
  abandoned_offsets: [messaging]
requires_clear:
  # a stall, a burst or abandoned offsets are causes only while the service's pods are fine
  consumer_stalled: [consumer_pods]
  traffic_burst: [consumer_pods]
  abandoned_offsets: [consumer_pods]
proposals:
  consumer_not_running: >
    Bring {consumergroup} back (scale it up, or fix what kills its pods); it picks up where its
    committed offsets left off, and the lag drains by itself. Nothing is lost.
  consumer_stalled: >
    Fix what {consumergroup}'s log says holds it (usually its database); it resumes on its own. If
    one message keeps failing, it goes to the dead-letter topic after its retries.
  traffic_burst: >
    Nothing to do while the lag falls; if it keeps up for long, scale {consumergroup} (its
    parallelism is capped by the topic's partitions).
  abandoned_offsets: >
    Confirm that {consumergroup} stopped reading this topic on purpose (kafka-consumer-groups.sh
    --describe --group {consumergroup} shows no consumer), then delete its committed offsets for it.
checks:
  messaging:
    tool: get_messaging_status
    arguments: {minutes: 15}
    tells: Every consumer group's lag per topic, how it changed in 15 minutes (falling or growing), and how many consumers each group has connected; plus the outboxes and dead letters.
  consumer_pods:
    tool: get_pod_status
    arguments: {workload: "{consumergroup}"}
    tells: Whether {consumergroup}'s pods are wanted, running and ready.
  consumer_errors:
    tool: get_recent_errors
    arguments: {workload: "{consumergroup}", minutes: 15}
    tells: What {consumergroup} logged, including errors handling its messages.
  database:
    tool: get_pod_status
    arguments: {workload: postgres}
    tells: Whether the database the services use is running and ready.
  kafka:
    tool: get_pod_status
    arguments: {workload: kafka}
    tells: Whether Kafka is running and ready.
---
# KafkaConsumerLag

## What it means

A consumer group, the service in the alert's `consumergroup` label, has been more than 500
messages behind on a topic (the `topic` label) for five minutes. What it has not read yet waits
in Kafka; nothing is lost, but whatever those messages drive is late: ledger commands (sagas wait),
replies (sagas wait), payment events (webhooks and notifications are late).

## Telling the causes apart

The messaging check shows the group's lag, its change over 15 minutes, and its connected consumers:

- **Not running**: the group has no consumers, and its service's pods are missing, crashing or not ready.
- **Stalled**: consumers are connected, but the lag does not fall; the service's log shows the
  error it keeps hitting.
- **A burst**: the lag is high but falling, and the service is healthy.
- **Abandoned offsets**: no consumer on that topic for that group, while the service is healthy and
  reads its other topics. The group stopped reading the topic and its old offsets stay behind.

## What resolves it

A running, healthy consumer: it continues from its committed offsets and the lag drains by itself.
Messages are processed once (consumers are idempotent). The alert resolves when the lag is under 500.
