#!/bin/bash
# The card processor answers every request with 503, while its pods keep running.
# Expected diagnosis: processor_outage.
#
#   evals/faults/processor-outage.sh start          # for 240 seconds
#   evals/faults/processor-outage.sh start 150
#   evals/faults/processor-outage.sh stop           # end it now (it also ends by itself)
#   evals/faults/processor-outage.sh wait-ended     # returns the moment the breaker has closed again
#                                                   # while the alert is still listed (for "just ended")
#
# Uses processor-simulator's own test hook (POST /v1/test/outage). The state is in its database,
# so every replica behaves the same.
source "$(dirname "$0")/lib.sh"

case "${1:-}" in
  start)
    echo "$(now) outage on: $(processor_post "/v1/test/outage?seconds=${2:-240}")" ;;
  stop)
    echo "$(now) outage off: $(processor_post "/v1/test/outage?seconds=0")" ;;
  wait-ended)
    # over = no payment-service instance reports its breaker open any more
    until [ "$(prom 'max(resilience4j_circuitbreaker_state{name="processor",state=~"open|half_open"})')" = "0" ]; do sleep 1; done
    if firing | grep -qw ProcessorCircuitOpen; then
      echo "$(now) the breaker is closed again and the alert is still listed"
    else
      echo "$(now) the breaker is closed again, but the alert has already cleared" >&2
      exit 1
    fi ;;
  *) echo "usage: $(basename "$0") start [seconds] | stop | wait-ended" >&2; exit 2 ;;
esac
