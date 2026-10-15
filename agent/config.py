"""Where the agent finds its tools and keeps its traces."""
import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """KEY=VALUE lines from the local .env file (git-ignored), for values that must not be in the
    code, such as AGENT_TOKEN. The real environment wins. (The MCP server reads the same file.)"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(Path(__file__).resolve().parent.parent / ".env")

# The model: llama.cpp's llama-server on this machine (scripts/start-llm.ps1), OpenAI-compatible.
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8090/v1")
# The model's name as the server knows it (its --alias). gpt-oss-20b since 2026-10-15: on the
# fresh scenarios it tied with qwen3.5-9b on correctness and safety (11 of 12 each) and was 2.4
# times faster (evals/results/20261015T132537Z-comparison.md). The others: qwen3.5-9b,
# gemma-4-26b-a4b.
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss-20b")
# Whether the model uses its built-in thinking before the diagnosis ("on" or "off"). Off: the
# answer's own reasoning field does that job, in a few seconds. The evals will compare the two.
DIAGNOSE_THINKING = os.environ.get("DIAGNOSE_THINKING", "off").lower() == "on"
# Whether llama-server may reuse what it computed for the previous prompt ("on" or "off"). Off:
# with it on, the same prompt does not always get the same answer (what was reused changes the
# arithmetic slightly, and at temperature 0 a near tie then falls the other way). Measured on the
# evals: the same prompts scored 12, 10 and 9 of 13 with it on, and two runs with it off were
# identical. On is about 15% faster, for when repeating an answer does not matter.
LLM_CACHE_PROMPT = os.environ.get("LLM_CACHE_PROMPT", "off").lower() == "on"
# The MCP server (python -m mcp_server.server). In the cluster this is its Service.
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8091/mcp")
# One Markdown file per alert the agent knows how to investigate.
RUNBOOKS_DIR = Path(os.environ.get("RUNBOOKS_DIR", Path(__file__).resolve().parent.parent / "runbooks"))
# The agent as a service (payflow-agent serve): where it listens, and what it keeps (git-ignored).
# 127.0.0.1 is this machine only; PayFlow's Alertmanager, in the cluster, needs 0.0.0.0.
AGENT_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8092"))
AGENT_DB = Path(os.environ.get("AGENT_DB", Path(__file__).resolve().parent.parent / "runs" / "agent.sqlite"))
# Where the service's paused workflows keep their state (LangGraph's checkpoints), so that a
# decision can resume one after a restart.
AGENT_CHECKPOINTS = Path(os.environ.get("AGENT_CHECKPOINTS", Path(__file__).resolve().parent.parent / "runs" / "checkpoints.sqlite"))
# When set, every request to the service must carry "Authorization: Bearer <this>".
AGENT_TOKEN = os.environ.get("AGENT_TOKEN") or None
# One JSON trace per investigation (git-ignored).
RUNS_DIR = Path(os.environ.get("RUNS_DIR", Path(__file__).resolve().parent.parent / "runs"))
