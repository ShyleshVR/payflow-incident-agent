#!/bin/bash
# Shared by the fault scripts in this folder. They act on a local PayFlow cluster and need
# PayFlow's repository for its load test and its local secrets. Run them from Git Bash.
export MSYS_NO_PATHCONV=1   # stops Git Bash rewriting container paths
PAYFLOW_DIR=${PAYFLOW_DIR:-/d/Payments-Engine}
NS=${PAYFLOW_NAMESPACE:-payflow}
PROMETHEUS=${PROMETHEUS_URL:-http://localhost:9090}
ALERTMANAGER=${ALERTMANAGER_URL:-http://localhost:9093}

# prom '<promql>': the first value of the result, or nothing
prom() {
  curl -s -m 10 -G "$PROMETHEUS/api/v1/query" --data-urlencode "query=$1" \
    | python -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '')" 2>/dev/null | tr -d '\r'
}

# firing: the names of the alerts firing now, on one line
firing() {
  curl -s -m 10 "$ALERTMANAGER/api/v2/alerts?active=true&silenced=false&inhibited=false" \
    | python -c "import sys,json; print(' '.join(sorted(set(a['labels']['alertname'] for a in json.load(sys.stdin)))))" 2>/dev/null | tr -d '\r'
}

processor_api_key() {
  grep '^processor-api-key=' "$PAYFLOW_DIR/k8s/overlays/local/secrets/auth.env" | cut -d= -f2-
}

# processor_post '<path and query>': a POST to processor-simulator's own API, from inside one of its pods
processor_post() {
  local pod; pod=$(kubectl -n "$NS" get pods -o name | grep processor-simulator | head -1)
  kubectl -n "$NS" exec "$pod" -c processor-simulator -- wget -q -O - --post-data '' \
    --header "X-Api-Key: $(processor_api_key)" "http://localhost:8086$1"
}

# sql <database> '<statement>': as the Postgres superuser
sql() {
  kubectl -n "$NS" exec postgres-0 -- psql -U postgres -d "$1" -tAc "$2" | tr -d '\r'
}

now() { date -u +%H:%M:%S; }

usage() { echo "usage: $(basename "$0") start|stop" >&2; exit 2; }
