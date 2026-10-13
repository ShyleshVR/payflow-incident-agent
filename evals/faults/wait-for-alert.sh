#!/bin/bash
# Waits until an alert is firing in Alertmanager.
#
#   evals/faults/wait-for-alert.sh ProcessorCircuitOpen        # up to 5 minutes
#   evals/faults/wait-for-alert.sh ProcessorCircuitOpen 600
source "$(dirname "$0")/lib.sh"
ALERT=${1:?usage: wait-for-alert.sh <AlertName> [seconds]}
END=$(( $(date +%s) + ${2:-300} ))

until firing | grep -qw "$ALERT"; do
  if [ "$(date +%s)" -ge "$END" ]; then
    echo "$(now) $ALERT did not fire. Firing now: $(firing)" >&2
    exit 1
  fi
  sleep 3
done
echo "$(now) firing: $(firing)"
