#!/bin/bash
# Three payment captures parked for an operator: SagaRequiresAttention, cause processor_no_answer.
# Expected: the agent proposes retrying the three; approved once the processor answers again,
# it retries them and they complete.
#
#   evals/faults/captures-parked.sh start     # the captures park about 10 minutes later
#   evals/faults/captures-parked.sh stop      # end the outage and retry the three by hand
#
# A new test merchant makes three payments with manual capture (authorized only). An 11-minute
# processor outage begins, then their captures are requested: each goes unanswered for the
# capture step's 10-minute timeout and parks CAPTURE_UNKNOWN. The outage ends about a minute
# later. No secret is printed.
source "$(dirname "$0")/lib.sh"
IDS="${TMPDIR:-/tmp}/payflow-captures-parked.ids"
AUTH="$PAYFLOW_DIR/k8s/overlays/local/secrets/auth.env"

json() { python -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1" | tr -d '\r'; }
token() { curl -s -m 15 -u "$1" -d grant_type=client_credentials http://localhost/oauth2/token | json "d['access_token']"; }
secret() { grep "^$1=" "$AUTH" | cut -d= -f2-; }

case "${1:-}" in
  start)
    admin=$(token "payflow-admin:$(secret admin-client-secret)")
    run=$(date +%s)
    merchant=$(curl -s -m 15 -X POST http://localhost/api/v1/merchants -H "Authorization: Bearer $admin" \
      -H 'Content-Type: application/json' -d "{\"name\":\"Agent test $run\",\"email\":\"agent-test-$run@example.test\"}")
    bearer=$(token "$(echo "$merchant" | json "d['credential']['clientId'] + ':' + d['credential']['clientSecret']")")
    : > "$IDS"
    for n in 1 2 3; do
      curl -s -m 15 -X POST http://localhost/api/v1/payments -H "Authorization: Bearer $bearer" -H 'Content-Type: application/json' \
        -H "Idempotency-Key: agent-test-$run-$n" \
        -d "{\"amount\":\"$((20 + n)).50\",\"currency\":\"USD\",\"paymentMethod\":\"pm_card_visa\",\"captureMethod\":\"MANUAL\"}" \
        | json "d['paymentId']" >> "$IDS"
    done
    sleep 5
    echo "$(now) outage on: $(processor_post "/v1/test/outage?seconds=660")"
    for id in $(cat "$IDS"); do
      echo "$(now) capture of $id requested: $(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST \
        "http://localhost/api/v1/payments/$id/capture" -H "Authorization: Bearer $bearer")"
    done
    echo "$(now) they park about 10 minutes from now; SagaRequiresAttention fires a minute after" ;;
  stop)
    echo "$(now) outage off: $(processor_post "/v1/test/outage?seconds=0")"
    [ -f "$IDS" ] || { echo "no captures recorded by start"; exit 0; }
    operator=$(token "incident-agent-operator:$(secret incident-agent-operator-client-secret)")
    for id in $(cat "$IDS"); do
      code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST "http://localhost/api/v1/payments/$id/saga/retry" \
        -H "Authorization: Bearer $operator")
      echo "$(now) retry of $id: $code$([ "$code" = 409 ] && echo " (it was not parked)")"
    done ;;
  *) usage ;;
esac
