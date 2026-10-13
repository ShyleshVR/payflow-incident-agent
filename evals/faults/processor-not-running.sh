#!/bin/bash
# The card processor has no pods: its deployment is scaled to zero.
# Expected diagnosis: processor_not_running.
#
#   evals/faults/processor-not-running.sh start
#   evals/faults/processor-not-running.sh stop
source "$(dirname "$0")/lib.sh"

case "${1:-}" in
  start)
    echo "$(now) $(kubectl -n "$NS" scale deployment processor-simulator --replicas=0)" ;;
  stop)
    kubectl -n "$NS" scale deployment processor-simulator --replicas=2
    kubectl -n "$NS" rollout status deployment processor-simulator --timeout=300s | tail -1 ;;
  *) usage ;;
esac
