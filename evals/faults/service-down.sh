#!/bin/bash
# A PayFlow service has no healthy instance: ServiceDown. Four ways, one per cause:
#
#   evals/faults/service-down.sh start <workload> zero            # scaled to zero          -> scaled_to_zero
#   evals/faults/service-down.sh start <workload> crash           # exits as it starts      -> crash_loop
#   evals/faults/service-down.sh start <workload> unschedulable   # asks for 64 GiB         -> not_schedulable
#   evals/faults/service-down.sh start <workload> unready         # its readiness never OK  -> not_ready
#   evals/faults/service-down.sh stop <workload>
#
# The broken ones scale the deployment to zero first and back with only broken pods (a rolling
# update would keep the old, working pods until a new one is ready, which never happens), as
# processor-crash-loop.sh does. stop puts the pod template back (rollout undo) and the replicas.
source "$(dirname "$0")/lib.sh"
WORKLOAD="${2:-}"
SAVED="${TMPDIR:-/tmp}/payflow-service-down.$WORKLOAD"
[ -n "$WORKLOAD" ] || usage

broken() {
  case "$1" in
    crash) echo '[{"op":"add","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo started with a broken command; exit 1"]}]' ;;
    # the limit too: a request above the limit is refused
    unschedulable) echo '[{"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"64Gi"},{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"64Gi"}]' ;;
    unready) echo '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/actuator/never-ready"}]' ;;
  esac
}

case "${1:-}" in
  start)
    how="${3:-zero}"
    replicas=$(kubectl -n "$NS" get deployment "$WORKLOAD" -o jsonpath='{.spec.replicas}')
    echo "$replicas $how" > "$SAVED"
    kubectl -n "$NS" scale deployment "$WORKLOAD" --replicas=0 > /dev/null
    if [ "$how" != zero ]; then
      patch=$(broken "$how")
      [ -n "$patch" ] || { echo "unknown way: $how" >&2; exit 2; }
      if ! kubectl -n "$NS" patch deployment "$WORKLOAD" --type=json -p "$patch" > /dev/null; then
        kubectl -n "$NS" scale deployment "$WORKLOAD" --replicas="$replicas" > /dev/null
        echo "the patch was refused; $WORKLOAD is back as it was" >&2
        exit 1
      fi
      kubectl -n "$NS" scale deployment "$WORKLOAD" --replicas="$replicas" > /dev/null
    fi
    echo "$(now) $WORKLOAD: $how" ;;
  stop)
    read -r replicas how < "$SAVED"
    [ "$how" = zero ] || kubectl -n "$NS" rollout undo deployment "$WORKLOAD" > /dev/null
    kubectl -n "$NS" scale deployment "$WORKLOAD" --replicas="${replicas:-2}" > /dev/null
    kubectl -n "$NS" rollout status deployment "$WORKLOAD" --timeout=300s | tail -1 ;;
  *) usage ;;
esac
