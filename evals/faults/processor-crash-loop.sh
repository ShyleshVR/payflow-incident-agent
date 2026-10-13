#!/bin/bash
# The card processor's pods exist but crash as soon as they start (a bad start command).
# Expected diagnosis: processor_not_running, from other evidence than "scaled to zero".
#
#   evals/faults/processor-crash-loop.sh start
#   evals/faults/processor-crash-loop.sh stop
#
# A rolling update alone would not do it: Kubernetes keeps the old, working pods until a new one
# is ready, which never happens. So the deployment is scaled to zero first and comes back with
# only the broken pods.
source "$(dirname "$0")/lib.sh"
BROKEN='[{"op":"add","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo started with a broken command; exit 1"]}]'

case "${1:-}" in
  start)
    kubectl -n "$NS" scale deployment processor-simulator --replicas=0 > /dev/null
    kubectl -n "$NS" patch deployment processor-simulator --type=json -p "$BROKEN" > /dev/null
    kubectl -n "$NS" scale deployment processor-simulator --replicas=2 > /dev/null
    echo "$(now) processor-simulator now starts with a command that exits at once" ;;
  stop)
    # back to the pod template before the patch
    kubectl -n "$NS" rollout undo deployment processor-simulator > /dev/null
    kubectl -n "$NS" scale deployment processor-simulator --replicas=2 > /dev/null
    kubectl -n "$NS" rollout status deployment processor-simulator --timeout=300s | tail -1
    if kubectl -n "$NS" get deployment processor-simulator -o jsonpath='{.spec.template.spec.containers[0].command}' | grep -q broken; then
      echo "the broken command is still in the deployment: remove it by hand" >&2
      exit 1
    fi ;;
  *) usage ;;
esac
