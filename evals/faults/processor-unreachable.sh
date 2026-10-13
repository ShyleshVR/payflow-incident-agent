#!/bin/bash
# The card processor is healthy, but payment-service cannot reach it: payment-service is given
# an address for the processor that does not exist. (The cluster has no network policies to cut
# the real path with, so the wrong address stands in for a broken route or a bad DNS name.)
# Expected diagnosis: processor_unreachable.
#
#   evals/faults/processor-unreachable.sh start
#   evals/faults/processor-unreachable.sh stop
#
# Both restart payment-service's pods, one at a time.
source "$(dirname "$0")/lib.sh"
RIGHT=http://processor-simulator:8086
WRONG=http://processor-simulator.nowhere:8086

set_address() {
  kubectl -n "$NS" set env deployment/payment-service PAYFLOW_PROCESSOR_BASE_URL="$1" > /dev/null
  kubectl -n "$NS" rollout status deployment payment-service --timeout=420s | tail -1
}

case "${1:-}" in
  start)
    set_address "$WRONG"
    echo "$(now) payment-service now looks for the processor at $WRONG" ;;
  stop)
    set_address "$RIGHT"
    echo "$(now) payment-service looks for the processor at $RIGHT again" ;;
  *) usage ;;
esac
