#!/bin/bash
# One pod of a service keeps dying while the others run: InstanceDown.
#
#   evals/faults/instance-down.sh start <workload> [minutes]   # 5 minutes by default
#   evals/faults/instance-down.sh stop <workload>
#
# Stops the service's first pod's process every 15 seconds (SIGTERM to its main process), so it
# restarts, backs off and is down long enough for the alert. stop ends that and deletes the pod
# (the deployment makes a fresh one).
source "$(dirname "$0")/lib.sh"
WORKLOAD="${2:-}"
SAVED="${TMPDIR:-/tmp}/payflow-instance-down.$WORKLOAD"
[ -n "$WORKLOAD" ] || usage

case "${1:-}" in
  start)
    pod=$(kubectl -n "$NS" get pods -o name | grep "^pod/$WORKLOAD-" | head -1 | cut -d/ -f2)
    [ -n "$pod" ] || { echo "no pod of $WORKLOAD" >&2; exit 1; }
    until_s=$(( $(date +%s) + 60 * ${3:-5} ))
    ( while [ "$(date +%s)" -lt "$until_s" ] && [ -f "$SAVED" ]; do
        kubectl -n "$NS" exec "$pod" -- kill 1 > /dev/null 2>&1
        sleep 15
      done ) &
    echo "$pod $!" > "$SAVED"
    echo "$(now) $pod: its process is stopped every 15 seconds" ;;
  stop)
    read -r pod loop < "$SAVED"
    rm -f "$SAVED"
    kill "$loop" 2>/dev/null
    kubectl -n "$NS" delete pod "$pod" --wait=false > /dev/null
    kubectl -n "$NS" rollout status deployment "$WORKLOAD" --timeout=300s | tail -1 ;;
  *) usage ;;
esac
