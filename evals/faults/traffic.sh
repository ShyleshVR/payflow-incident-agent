#!/bin/bash
# Light traffic through PayFlow, in the background: payments at a steady rate for a few minutes.
# Most faults only show under traffic: the circuit breaker opens when calls fail, and with no
# payments there are no calls. Returns once processor calls are flowing.
#
#   evals/faults/traffic.sh            # 5 payments a second for 5 minutes
#   evals/faults/traffic.sh 8 10       # 8 minutes at 10 a second
#
# Uses PayFlow's own load test (scripts/load-test.sh, k6 in the cluster). That script prints FAIL
# when a fault is running, because payments cannot complete in time: expected here. It writes two
# result files into PayFlow's load-tests/results; this script deletes them when the traffic ends.
source "$(dirname "$0")/lib.sh"
MINUTES=${1:-5}
RATE=${2:-5}

(
  cd "$PAYFLOW_DIR" || exit 1
  started=$(date -u +%Y%m%dT%H%M)
  bash scripts/load-test.sh step "$RATE" "${MINUTES}m" > /dev/null 2>&1
  # the run's id begins with the minute it started (or the next one)
  for stamp in "$started" "$(date -u -d '+1 minute' +%Y%m%dT%H%M 2>/dev/null)"; do
    [ -n "$stamp" ] && rm -f load-tests/results/"$stamp"*-step-"$RATE".json load-tests/results/"$stamp"*-step-"$RATE".log
  done
) &

echo "$(now) traffic starting: $RATE payments a second for $MINUTES minutes"
# flowing = processor calls in the last 20 seconds, looked at only once 20 seconds have passed, so
# that they are this traffic's. (Not a total compared with the one at the start: after a restart
# of payment-service its new pods count from zero, and the total stays below the old one for minutes.)
sleep 20
for _ in $(seq 1 80); do
  count=$(prom 'sum(increase(processor_calls_seconds_count[20s]))')
  if [ -n "$count" ] && [ "$(python -c "print(1 if float('$count') > 20 else 0)")" = 1 ]; then
    echo "$(now) traffic is flowing"
    exit 0
  fi
  sleep 2
done
echo "$(now) no processor calls seen after 3 minutes: is the cluster up?" >&2
exit 1
