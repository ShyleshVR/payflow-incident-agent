#!/bin/bash
# A message no consumer can read, published to a PayFlow topic: DeadLettersArriving.
#
#   evals/faults/dead-letter.sh start [topic]     # payment-created by default
#
# The consumers (notification-service and webhook-service, for payment-created) cannot parse it,
# and send it to <topic>.DLT at once (it is not retried). Nothing to undo: the dead letter stays
# for inspection, and the alert clears ten minutes later.
source "$(dirname "$0")/lib.sh"

case "${1:-}" in
  start)
    topic="${2:-payment-created}"
    kubectl -n "$NS" exec kafka-0 -- sh -c "echo 'this is not a payment event' | /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic $topic" \
      && echo "$(now) one unreadable message published to $topic" ;;
  stop) echo "nothing to undo" ;;
  *) usage ;;
esac
