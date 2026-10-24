#!/bin/bash
# KafkaUnreachable, three ways:
#
#   evals/faults/kafka-down.sh start zero        # Kafka's statefulset scaled to zero  -> kafka_not_running
#   evals/faults/kafka-down.sh start crash       # kafka-0 exits as it starts          -> kafka_not_running
#   evals/faults/kafka-down.sh start exporter    # only kafka-exporter scaled to zero  -> exporter_down
#   evals/faults/kafka-down.sh stop
#
# Every service keeps its messages in its outbox meanwhile; they drain once Kafka is back.
source "$(dirname "$0")/lib.sh"
SAVED="${TMPDIR:-/tmp}/payflow-kafka-down"
BROKEN='[{"op":"add","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo started with a broken command; exit 1"]}]'

case "${1:-}" in
  start)
    how="${2:-zero}"
    echo "$how" > "$SAVED"
    case "$how" in
      zero) kubectl -n "$NS" scale statefulset kafka --replicas=0 > /dev/null ;;
      crash)
        kubectl -n "$NS" scale statefulset kafka --replicas=0 > /dev/null
        until [ -z "$(kubectl -n "$NS" get pods -o name | grep '^pod/kafka-0$')" ]; do sleep 2; done
        kubectl -n "$NS" patch statefulset kafka --type=json -p "$BROKEN" > /dev/null
        kubectl -n "$NS" scale statefulset kafka --replicas=1 > /dev/null ;;
      exporter) kubectl -n "$NS" scale deployment kafka-exporter --replicas=0 > /dev/null ;;
      *) echo "unknown way: $how" >&2; exit 2 ;;
    esac
    echo "$(now) kafka: $how" ;;
  stop)
    how=$(cat "$SAVED" 2>/dev/null || echo zero)
    case "$how" in
      exporter)
        kubectl -n "$NS" scale deployment kafka-exporter --replicas=1 > /dev/null
        kubectl -n "$NS" rollout status deployment kafka-exporter --timeout=300s | tail -1 ;;
      *)
        if [ "$how" = crash ]; then
          kubectl -n "$NS" patch statefulset kafka --type=json -p '[{"op":"remove","path":"/spec/template/spec/containers/0/command"}]' > /dev/null
          kubectl -n "$NS" delete pod kafka-0 --wait=false > /dev/null 2>&1
        fi
        kubectl -n "$NS" scale statefulset kafka --replicas=1 > /dev/null
        kubectl -n "$NS" rollout status statefulset kafka --timeout=300s | tail -1 ;;
    esac ;;
  *) usage ;;
esac
