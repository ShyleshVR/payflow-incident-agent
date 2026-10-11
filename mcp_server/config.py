"""Where the MCP server finds PayFlow. Defaults are the local cluster's localhost ports."""
import os

ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
# Kubernetes is reached through the kubeconfig (or the pod's ServiceAccount in the cluster).
NAMESPACE = os.environ.get("PAYFLOW_NAMESPACE", "payflow")

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8091"))
