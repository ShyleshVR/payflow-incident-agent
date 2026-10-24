#!/bin/bash
# Three payouts the bank declines within minutes: PayoutsFailing.
#
#   evals/faults/payouts-failing.sh start [bank account]   # ba_test_invalid (default) or ba_test_closed
#   evals/faults/payouts-failing.sh stop
#
# A payout only pays money settled at least payflow.payouts.delay ago (2 days), so a new merchant
# has nothing to pay out. start sets the delay to zero on payout-service (on this local cluster
# only; stop removes it), makes a test merchant whose payout destination is the simulator's test
# account (ba_test_invalid: the bank answers invalid_account), takes three payments, and asks for
# three instant payouts. The bank declines each; the money goes back to the merchant's balance.
# No secret is printed.
source "$(dirname "$0")/lib.sh"
ACCOUNT="${2:-ba_test_invalid}"

json() { python -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1" | tr -d '\r'; }
token() { curl -s -m 15 -u "$1" -d grant_type=client_credentials http://localhost/oauth2/token | json "d['access_token']"; }

case "${1:-}" in
  start)
    kubectl -n "$NS" set env deployment/payout-service PAYFLOW_PAYOUTS_DELAY=PT0S > /dev/null
    kubectl -n "$NS" rollout status deployment payout-service --timeout=300s | tail -1
    secret=$(grep '^admin-client-secret=' "$PAYFLOW_DIR/k8s/overlays/local/secrets/auth.env" | cut -d= -f2-)
    admin=$(token "payflow-admin:$secret")
    run=$(date +%s)
    merchant=$(curl -s -m 15 -X POST http://localhost/api/v1/merchants -H "Authorization: Bearer $admin" \
      -H 'Content-Type: application/json' -d "{\"name\":\"Payout test $run\",\"email\":\"payout-test-$run@example.test\"}")
    bearer=$(token "$(echo "$merchant" | json "d['credential']['clientId'] + ':' + d['credential']['clientSecret']")")
    echo "$(now) destination: $(curl -s -m 15 -o /dev/null -w '%{http_code}' -X PUT http://localhost/api/v1/payouts/destination \
      -H "Authorization: Bearer $bearer" -H 'Content-Type: application/json' -d "{\"bankAccount\":\"$ACCOUNT\"}") ($ACCOUNT)"
    for n in 1 2 3; do
      curl -s -m 15 -o /dev/null -X POST http://localhost/api/v1/payments -H "Authorization: Bearer $bearer" \
        -H 'Content-Type: application/json' -H "Idempotency-Key: payout-test-$run-pay-$n" \
        -d "{\"amount\":\"$((30 + n)).00\",\"currency\":\"USD\",\"paymentMethod\":\"pm_card_visa\"}"
    done
    # settled: captured and booked to the merchant's balance by the saga
    for i in $(seq 1 30); do
      payable=$(curl -s -m 15 "http://localhost/api/v1/payouts/balance?currency=USD" -H "Authorization: Bearer $bearer" | json "d['payable']")
      python -c "import sys; sys.exit(0 if float('$payable') >= 90 else 1)" 2>/dev/null && break
      sleep 2
    done
    echo "$(now) payable: $payable USD"
    for n in 1 2 3; do
      echo "$(now) instant payout $n: $(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST http://localhost/api/v1/payouts \
        -H "Authorization: Bearer $bearer" -H 'Content-Type: application/json' -H "Idempotency-Key: payout-test-$run-po-$n" \
        -d '{"amount":"25.00","currency":"USD"}')"
    done
    echo "$(now) the bank declines them within seconds; PayoutsFailing fires at the next evaluation" ;;
  stop)
    kubectl -n "$NS" set env deployment/payout-service PAYFLOW_PAYOUTS_DELAY- > /dev/null
    kubectl -n "$NS" rollout status deployment payout-service --timeout=300s | tail -1 ;;
  *) usage ;;
esac
