#!/bin/bash
# A day's reconciliation fails because a source it reads is down: ReconciliationRunFailed.
# Expected cause, by the workload given: processor-simulator -> processor_unavailable,
# ledger-service -> ledger_unavailable, payment-service -> payments_unavailable,
# payout-service -> payouts_unavailable.
#
#   evals/faults/reconciliation-source-down.sh start ledger-service [day]   # day: yesterday (UTC) by default
#   evals/faults/reconciliation-source-down.sh stop ledger-service
#
# start: the day's reconciliation runs are deleted (on this local cluster only: as if the day had
# never been reconciled; a run of it makes them again), the workload is scaled to zero, and the day
# is reconciled by hand: the run fails, and the alert fires at once (its rule has no "for").
# stop: the workload comes back. The day stays unreconciled until a run of it completes: the
# agent's approved rerun, the hourly catch-up, or a run by hand. No secret is printed.
source "$(dirname "$0")/lib.sh"
WORKLOAD="${2:-}"
DAY="${3:-$(date -u -d yesterday +%F)}"
SAVED="${TMPDIR:-/tmp}/payflow-reconciliation-source-down.$WORKLOAD"
case "$WORKLOAD" in processor-simulator|ledger-service|payment-service|payout-service) ;;
  *) echo "usage: $(basename "$0") start|stop processor-simulator|ledger-service|payment-service|payout-service [day]" >&2; exit 2 ;;
esac

json() { python -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1" | tr -d '\r'; }
admin_token() {
  local secret; secret=$(grep '^admin-client-secret=' "$PAYFLOW_DIR/k8s/overlays/local/secrets/auth.env" | cut -d= -f2-)
  curl -s -m 15 -u "payflow-admin:$secret" -d grant_type=client_credentials http://localhost/oauth2/token | json "d['access_token']"
}

case "${1:-}" in
  start)
    token=$(admin_token)
    sql recon_db "DELETE FROM reconciliation_discrepancy WHERE run_id IN (SELECT id FROM reconciliation_run WHERE business_date = '$DAY');
                  DELETE FROM reconciliation_run WHERE business_date = '$DAY';" >/dev/null
    echo "$(now) the runs of $DAY are deleted: it is not reconciled"
    kubectl -n "$NS" get deployment "$WORKLOAD" -o jsonpath='{.spec.replicas}' > "$SAVED"
    echo "$(now) $(kubectl -n "$NS" scale deployment "$WORKLOAD" --replicas=0)"
    until [ -z "$(kubectl -n "$NS" get pods -o name | grep "^pod/$WORKLOAD-")" ]; do sleep 2; done
    echo "$(now) $WORKLOAD has no pods"
    answer=$(curl -s -m 120 -X POST http://localhost/api/v1/reconciliation/runs -H "Authorization: Bearer $token" \
      -H 'Content-Type: application/json' -d "{\"date\":\"$DAY\"}")
    echo "$(now) the run of $DAY: $(echo "$answer" | json "d['run']['status'] + ': ' + str(d['run']['error'])[:160]")" ;;
  stop)
    kubectl -n "$NS" scale deployment "$WORKLOAD" --replicas="$(cat "$SAVED" 2>/dev/null || echo 2)"
    kubectl -n "$NS" rollout status deployment "$WORKLOAD" --timeout=300s | tail -1 ;;
  *) usage ;;
esac
