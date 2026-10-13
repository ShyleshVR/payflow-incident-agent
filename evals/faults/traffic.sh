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

before=$(prom 'sum(processor_calls_seconds_count)')
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
# flowing = the count of processor calls has grown since we started (not: some earlier traffic's rate)
for _ in $(seq 1 90); do
  count=$(prom 'sum(processor_calls_seconds_count)')
  if [ -n "$count" ] && [ "$(python -c "print(1 if float('$count') - float('${before:-0}' or 0) > 30 else 0)")" = 1 ]; then
    echo "$(now) traffic is flowing"
    exit 0
  fi
  sleep 2
done
echo "$(now) no processor calls seen after 3 minutes: is the cluster up?" >&2
exit 1
