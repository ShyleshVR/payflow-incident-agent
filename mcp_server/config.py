"""Where the MCP server finds PayFlow. Defaults are the local cluster's localhost ports."""
import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """KEY=VALUE lines from a local .env file (git-ignored), for values that must not be in the
    code. The real environment wins: in the cluster a Secret provides them and there is no file."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(Path(__file__).resolve().parent.parent / ".env")

ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
# Kubernetes is reached through the kubeconfig (or the pod's ServiceAccount in the cluster).
NAMESPACE = os.environ.get("PAYFLOW_NAMESPACE", "payflow")

# PayFlow's own API, through its gateway, as the read-only service client "incident-agent"
# (it can look at sagas and reconciliation runs; it can retry or start nothing).
PAYFLOW_URL = os.environ.get("PAYFLOW_URL", "http://localhost")
PAYFLOW_CLIENT_ID = os.environ.get("PAYFLOW_CLIENT_ID", "incident-agent")
# No default: scripts/write_env.py copies it from PayFlow's local secrets into .env.
PAYFLOW_CLIENT_SECRET = os.environ.get("PAYFLOW_CLIENT_SECRET", "")

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8091"))
