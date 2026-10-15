---
alert: ProcessorCircuitOpen
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", ProcessorCircuitOpen
causes:
  processor_outage: The processor is failing or switched off on its side. Its pods are running, but it answers with 5xx, or its own log says an outage began.
  processor_not_running: The processor's pods are missing, crashing, unschedulable or not ready.
  processor_dependency_down: The processor is running but cannot work because something it needs is down, typically its database.
  processor_unreachable: The processor looks healthy by its own pods, health and log, but payment-service cannot reach it or times out calling it.
# For each cause, the checks whose result can show it. A diagnosis must quote one of them, from a
# check that found something (a logged line, or a concern); otherwise the cause is not reported
# and the answer is unknown. processor_calls is in none: calls go unanswered whatever the cause.
# processor_unreachable is shown by payment-service's own log, which names each failed call
# (since PayFlow logs them): the processor's checks can only show that nothing is wrong there.
shown_by:
  processor_outage: [processor_errors, processor_health]
  processor_not_running: [processor_pods, processor_health]
  processor_dependency_down: [processor_errors, processor_health, database]
  processor_unreachable: [payment_service_errors]
# Checks that must have run and found nothing for a cause to stand. payment-service logs its failed
# calls whatever the cause, so they mean "unreachable" only while the processor itself looks fine.
requires_clear:
  processor_unreachable: [processor_pods, processor_health]
# The next step the agent proposes for each cause; a person approves or rejects it. (Approving
# records the decision; nothing is carried out by the agent yet.)
proposals:
  processor_outage: >
    Contact the card processor about the outage on their side and follow its status. The breaker
    closes by itself once trial calls succeed; then retry the sagas that parked during the outage.
  processor_not_running: >
    Bring the processor's pods back: scale the deployment up, or fix what makes them crash (their
    last exit reason and events say what). Then retry the sagas that parked meanwhile.
  processor_dependency_down: >
    Restore what the processor depends on, usually its database login or connection pool (its log
    names the failure). Then retry the sagas that parked meanwhile.
  processor_unreachable: >
    Restore payment-service's path to the processor: its configured address, the DNS name, or the
    network between them. Then retry the sagas that parked meanwhile.
checks:
  processor_calls:
    tool: get_dependency_status
    arguments: {minutes: 15}
    tells: How payment-service's calls to the card processor ended in the last 15 minutes (answered or not, per operation), and the circuit breaker's state.
  processor_pods:
    tool: get_pod_status
    arguments: {workload: processor-simulator}
    tells: Whether the processor's pods are running and ready, or crashing, unschedulable or missing.
  processor_health:
    tool: get_service_health
    arguments: {service: processor-simulator}
    tells: Whether the processor is up and how it answers the requests it receives (share of 5xx, response time).
  processor_errors:
    tool: get_recent_errors
    arguments: {workload: processor-simulator, minutes: 15}
    tells: What the processor itself logged, such as an outage being switched on or its database being unreachable.
  payment_service_errors:
    tool: get_recent_errors
    arguments: {workload: payment-service, minutes: 15}
    tells: What payment-service logged while calling the processor (timeouts, refused connections, 5xx answers).
  payment_service_health:
    tool: get_service_health
    arguments: {service: payment-service}
    tells: Whether payment-service itself is up and healthy.
  database:
    tool: get_pod_status
    arguments: {workload: postgres}
    tells: Whether the database is running; the processor cannot answer without it.
  sagas:
    tool: list_sagas_needing_attention
    arguments: {}
    tells: Which payments are already parked or stuck because their processor calls went unanswered.
---
# ProcessorCircuitOpen

## What it means

payment-service calls the card processor to authorize, capture, void and refund. Each
payment-service instance has a circuit breaker around those calls. It opens when at least half
of the last 20 calls got no answer that can be trusted (a timeout, a refused connection, a 5xx).
While open it refuses calls without sending them, and every 10 seconds it lets 3 trial calls
through to see whether the processor is back. The alert fires when a breaker has been open for
a minute.

A declined card is an answer, not a failure: declines never open the breaker.

So the alert says one thing: **payment-service is not getting answers from the processor.** It
does not say why.

## Telling the causes apart

- **The processor is failing or switched off on its side.** Its pods are running, but it
  answers with 5xx, or its own log says an outage began. Calls from payment-service are
  unanswered across operations.
- **The processor is not running.** Its pods are missing, crashing or not ready. Its health
  shows no instance up.
- **The processor is up but cannot work.** It is running and reachable, but logs errors of its
  own, typically its database being unreachable. Check the database.
- **payment-service cannot reach a healthy processor.** The processor's pods, health and log
  look normal, while payment-service logs connection errors or timeouts. The fault is between
  them, or in payment-service.

If the breaker is closed again and calls are being answered, the outage is over: say so, and say
how long it lasted and what it left behind.

## What happens to payments meanwhile

- New payments stay in PROCESSING.
- An authorization that gets no answer for 2 minutes is reversed, and the payment fails with
  `processor_unavailable`. The customer is not charged.
- A capture that gets no answer for 10 minutes parks its saga for an operator, because the money
  may have moved. These show up as parked sagas and need a retry once the processor is back.
- Nothing is lost or charged twice: every retry reuses the same idempotency key.

## What resolves it

The breaker closes by itself as soon as trial calls succeed. Nothing in PayFlow needs to be
restarted for that. What needs a person is the processor itself (or the path to it), and
afterwards any saga that parked during the outage.
