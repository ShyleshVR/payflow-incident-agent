#!/bin/bash
# A message in payment-service's outbox that Kafka will never take: OutboxMessageParked.
#
#   evals/faults/outbox-message-parked.sh start too-large   # a PAYMENT_CREATED message of 1.3 MB (Kafka takes 1 MB)
#   evals/faults/outbox-message-parked.sh start not-json    # one whose payload is cut off: not JSON
#   evals/faults/outbox-message-parked.sh stop              # delete it
#
# The message is added to the outbox table as if payment-service had written it, for a payment that
# does not exist (so it holds back no real payment's messages). payment-service's own publisher
# tries it, gets RecordTooLargeException (or Jackson's JsonParseException), parks it as FAILED and
# logs why; outbox_failed goes to 1 and the alert fires a minute later. No traffic needed.
source "$(dirname "$0")/lib.sh"
SAVED="${TMPDIR:-/tmp}/payflow-outbox-message-parked"

case "${1:-}" in
  start)
    case "${2:-}" in
      too-large) tail="',\"description\":\"' || repeat('x', 1300000) || '\"}'" ;;
      not-json)  tail="',\"description\":\"order 4471'" ;;
      *) echo "start too-large|not-json" >&2; exit 2 ;;
    esac
    id=$(sql payflow "INSERT INTO outbox_event (id, aggregate_id, aggregate_type, event_type, payload, status, created_at, topic)
      SELECT gen_random_uuid(), gen_random_uuid(), 'PAYMENT', 'PAYMENT_CREATED',
             '{\"paymentId\":\"' || gen_random_uuid() || '\",\"amount\":12.00,\"currency\":\"USD\"' || $tail,
             'PENDING', now() AT TIME ZONE 'UTC', 'payment-created'
      RETURNING id" | head -1)
    echo "$id" > "$SAVED"
    echo "$(now) outbox message $id added ($2); waiting for payment-service to park it"
    for _ in $(seq 1 30); do
      status=$(sql payflow "SELECT status FROM outbox_event WHERE id = '$id'")
      [ "$status" = "FAILED" ] && { echo "$(now) parked: $(sql payflow "SELECT left(last_error, 160) FROM outbox_event WHERE id = '$id'")"; exit 0; }
      sleep 2
    done
    echo "$(now) not parked after a minute (status $status)" >&2; exit 1 ;;
  stop)
    [ -f "$SAVED" ] || { echo "nothing saved: was start run?" >&2; exit 1; }
    id=$(cat "$SAVED")
    echo "$(now) deleted $(sql payflow "WITH gone AS (DELETE FROM outbox_event WHERE id = '$id' RETURNING 1) SELECT count(*) FROM gone") outbox message(s)"
    rm -f "$SAVED" ;;
  *) usage ;;
esac
