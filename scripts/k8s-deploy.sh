#!/bin/bash
# The incident agent in PayFlow's local cluster (Docker Desktop), from Git Bash.
#
#   scripts/k8s-deploy.sh up       # build the image, make the Secrets from .env, apply deploy/k8s, wait
#   scripts/k8s-deploy.sh host     # back to development: the agent runs on this machine (payflow-agent serve)
#   scripts/k8s-deploy.sh status   # what runs where
#
# Needs: PayFlow's cluster up, .env written by scripts/write_env.py (PAYFLOW_CLIENT_SECRET,
# PAYFLOW_OPERATOR_CLIENT_SECRET, AGENT_TOKEN), and the model server on this machine
# (scripts/start-llm.ps1). PayFlow's Alertmanager always sends to
# incident-agent.payflow-agent.svc.cluster.local:8092: `up` makes that the agent's pod, `host`
# makes it this machine. Stop a `payflow-agent serve` running here before `up`: both want port 8092.
# No secret is printed.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
NS=payflow-agent
IMAGE=payflow-incident-agent:local

context=$(kubectl config current-context)
[ "$context" = docker-desktop ] || { echo "kubectl points at '$context', not docker-desktop: refusing" >&2; exit 1; }

env_value() {   # env_value KEY: its value in .env, or nothing
  [ -f .env ] && grep -m1 "^$1=" .env | cut -d= -f2- | tr -d '\r' || true
}

drop_service_of_other_type() {   # the one Service name is a LoadBalancer (up) or an ExternalName (host)
  local type
  type=$(kubectl -n $NS get service incident-agent -o jsonpath='{.spec.type}' 2>/dev/null || true)
  if [ -n "$type" ] && [ "$type" != "$1" ]; then kubectl -n $NS delete service incident-agent; fi
}

case "${1:-}" in
  up)
    for key in PAYFLOW_CLIENT_SECRET PAYFLOW_OPERATOR_CLIENT_SECRET AGENT_TOKEN; do
      [ -n "$(env_value $key)" ] || { echo ".env has no $key: run scripts/write_env.py first" >&2; exit 1; }
    done
    echo "==> building $IMAGE"
    docker build -q -f deploy/Dockerfile -t $IMAGE . > /dev/null

    echo "==> secrets"
    kubectl apply -f deploy/k8s/namespace.yaml > /dev/null
    secrets=$(mktemp -d); trap 'rm -rf "$secrets"' EXIT
    printf 'PAYFLOW_CLIENT_SECRET=%s\n' "$(env_value PAYFLOW_CLIENT_SECRET)" > "$secrets/mcp-server.env"
    printf 'AGENT_TOKEN=%s\nPAYFLOW_OPERATOR_CLIENT_SECRET=%s\n' \
      "$(env_value AGENT_TOKEN)" "$(env_value PAYFLOW_OPERATOR_CLIENT_SECRET)" > "$secrets/incident-agent.env"
    for name in mcp-server incident-agent; do
      file="$secrets/$name.env"
      command -v cygpath > /dev/null && file=$(cygpath -w "$file")     # kubectl.exe needs a Windows path
      kubectl -n $NS create secret generic $name --from-env-file="$file" --dry-run=client -o yaml \
        | kubectl apply -f - > /dev/null
    done

    echo "==> applying deploy/k8s"
    drop_service_of_other_type LoadBalancer
    kubectl apply -k deploy/k8s
    # the tag stays :local, so a new image needs new pods
    kubectl -n $NS rollout restart deployment mcp-server incident-agent > /dev/null
    kubectl -n $NS rollout status deployment mcp-server --timeout=180s
    kubectl -n $NS rollout status deployment incident-agent --timeout=180s
    echo "==> the agent answers on http://127.0.0.1:8092 (payflow-agent pending | show N | approve | reject)" ;;
  host)
    if kubectl -n $NS get deployment incident-agent > /dev/null 2>&1; then
      kubectl -n $NS scale deployment incident-agent mcp-server --replicas=0
    fi
    drop_service_of_other_type ExternalName
    kubectl apply -f deploy/k8s-host/agent-on-host.yaml
    echo "==> alerts now go to this machine: start the MCP server and payflow-agent serve here" ;;
  status)
    kubectl -n $NS get deployments,services,pvc 2>/dev/null || echo "nothing deployed in $NS" ;;
  *)
    echo "usage: $(basename "$0") up|host|status" >&2; exit 2 ;;
esac
