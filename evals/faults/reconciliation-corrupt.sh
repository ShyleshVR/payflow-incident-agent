#!/bin/bash
# A day's data corrupted on purpose, then reconciled: ReconciliationDiscrepancies. As PayFlow's own
# reconciliation test did (docs/RECONCILIATION.md, "On the cluster"); on this local cluster only.
#
#   evals/faults/reconciliation-corrupt.sh start amount [day]       # a capture +1.00 at the processor -> SETTLEMENT_AMOUNT_MISMATCH
#   evals/faults/reconciliation-corrupt.sh start settlement [day]   # a settlement moved out of the day -> CAPTURED_NOT_SETTLED
#   evals/faults/reconciliation-corrupt.sh stop                     # put the value back and reconcile the day again
#
# day: 2026-10-22 by default (it has captured payments). What was changed is saved, so stop undoes it.
source "$(dirname "$0")/lib.sh"
SAVED="${TMPDIR:-/tmp}/payflow-reconciliation-corrupt"

json() { python -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1" | tr -d '\r'; }
reconcile() {
  local secret token
  secret=$(grep '^admin-client-secret=' "$PAYFLOW_DIR/k8s/overlays/local/secrets/auth.env" | cut -d= -f2-)
  token=$(curl -s -m 15 -u "payflow-admin:$secret" -d grant_type=client_credentials http://localhost/oauth2/token | json "d['access_token']")
  curl -s -m 300 -X POST http://localhost/api/v1/reconciliation/runs -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' -d "{\"date\":\"$1\"}" \
    | json "d['run']['status'] + ', ' + str(d['run']['discrepancyCount']) + ' discrepancy(ies): ' + ', '.join(sorted({x['type'] for x in d['discrepancies']}))"
}

case "${1:-}" in
  start)
    how="${2:-amount}"; day="${3:-2026-10-22}"
    case "$how" in
      amount)
        row=$(sql processor_db "SELECT id || ' ' || captured_amount FROM authorizations WHERE status = 'CAPTURED' AND captured_at::date = '$day' ORDER BY captured_at LIMIT 1")
        [ -n "$row" ] || { echo "no capture on $day" >&2; exit 1; }
        echo "amount $day $row" > "$SAVED"
        sql processor_db "UPDATE authorizations SET captured_amount = captured_amount + 1 WHERE id = '${row%% *}'" > /dev/null
        echo "$(now) authorization ${row%% *}: captured amount ${row#* } + 1.00 at the processor" ;;
      settlement)
        row=$(sql ledger_db "SELECT id || ' ' || created_at FROM ledger_transactions WHERE type = 'SETTLEMENT' AND created_at::date = '$day' ORDER BY created_at LIMIT 1")
        [ -n "$row" ] || { echo "no settlement on $day" >&2; exit 1; }
        echo "settlement $day $row" > "$SAVED"
        sql ledger_db "UPDATE ledger_transactions SET created_at = created_at - interval '5 days' WHERE id = '${row%% *}'" > /dev/null
        echo "$(now) settlement ${row%% *} moved 5 days earlier, out of $day's window" ;;
      *) echo "unknown way: $how" >&2; exit 2 ;;
    esac
    echo "$(now) the run of $day: $(reconcile "$day")" ;;
  stop)
    read -r how day id rest < "$SAVED"
    case "$how" in
      amount) sql processor_db "UPDATE authorizations SET captured_amount = $rest WHERE id = '$id'" > /dev/null ;;
      settlement) sql ledger_db "UPDATE ledger_transactions SET created_at = '$rest' WHERE id = '$id'" > /dev/null ;;
    esac
    echo "$(now) put back; the run of $day: $(reconcile "$day")" ;;
  *) usage ;;
esac
