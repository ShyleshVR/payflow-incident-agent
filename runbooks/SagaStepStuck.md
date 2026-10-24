---
alert: SagaStepStuck
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", SagaStepStuck; the saga state machines (which steps wait on what)
causes:
  ledger_not_answering: Sagas wait in a ledger step (settling, holding, finalizing, releasing) because the ledger does not reply; it is down or does not read its commands.
  kafka_unreachable: Sagas wait because their commands or the replies cannot travel at all; Kafka is down, so the commands sit in the outbox.
  replies_not_read: The ledger replied, but the service whose saga waits does not read its replies (its consumer is down or stalled).
# {application} is the service whose sagas are stuck (payment-service or payout-service).
shown_by:
  ledger_not_answering: [stuck_sagas, ledger_pods, messaging]
  kafka_unreachable: [kafka_pods, messaging]
  replies_not_read: [messaging, service_errors]
requires_clear:
  # the ledger is the cause only while Kafka carries messages; replies unread only while the ledger runs
  ledger_not_answering: [kafka_pods]
  replies_not_read: [ledger_pods, kafka_pods]
proposals:
  ledger_not_answering: >
    Bring the ledger back (or fix what stops it reading ledger-commands); the waiting steps get
    their replies and the sagas continue by themselves. Re-sent commands are processed once.
  kafka_unreachable: >
    Bring Kafka back; the outboxes publish what waited, and the sagas continue by themselves.
  replies_not_read: >
    Fix {application}'s reply consumer (its log says why it does not read ledger replies); the
    replies wait in Kafka and are processed once it reads again.
checks:
  stuck_sagas:
    tool: list_sagas_needing_attention
    arguments: {}
    tells: The sagas in the same step for over 15 minutes (and those parked), with the step each is in and for how long.
  ledger_pods:
    tool: get_pod_status
    arguments: {workload: ledger-service}
    tells: Whether the ledger's pods are running and ready.
  ledger_errors:
    tool: get_recent_errors
    arguments: {workload: ledger-service, minutes: 15}
    tells: What the ledger logged, including errors handling its commands.
  messaging:
    tool: get_messaging_status
    arguments: {minutes: 15}
    tells: Each consumer group's lag (ledger-service on ledger-commands; the services on their ledger replies), and whether the outboxes drain.
  kafka_pods:
    tool: get_pod_status
    arguments: {workload: kafka}
    tells: Whether Kafka is running and ready.
  service_errors:
    tool: get_recent_errors
    arguments: {workload: "{application}", minutes: 15}
    tells: What {application} logged, including failures to read the ledger's replies or to publish its commands.
---
# SagaStepStuck

## What it means

A saga, PayFlow's step-by-step record of one payment, refund or payout, has been in the same step
for over 15 minutes, for five minutes. Steps take seconds. The steps that can wait that long are the
ones that wait on the ledger: the service sends a command through Kafka and waits for the reply. (A
call to the processor cannot: an authorization gives up after 2 minutes, a capture parks after 10;
a payout waiting on the bank is not counted.)

## Telling the causes apart

- **The ledger does not answer**: the ledger's pods are down, or it is far behind on
  `ledger-commands` (its lag grows) while Kafka is fine.
- **Kafka is down**: Kafka's pod is missing or failing; the services' outboxes pile up.
- **Replies not read**: the ledger works and has replied, but the waiting service's consumer is
  behind on its ledger replies.

## What resolves it

Restoring what the steps wait on. The sagas continue by themselves: commands are re-sent until a
reply arrives, and the ledger processes each command once. Nothing needs to be retried by hand.
The alert resolves when no step has waited over 15 minutes.
