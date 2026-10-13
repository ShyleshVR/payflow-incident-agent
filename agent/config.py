"""Where the agent finds its tools and keeps its traces."""
import os
from pathlib import Path

# The model: llama.cpp's llama-server on this machine (scripts/start-llm.ps1), OpenAI-compatible.
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8090/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5-9b")
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
# One JSON trace per investigation (git-ignored).
RUNS_DIR = Path(os.environ.get("RUNS_DIR", Path(__file__).resolve().parent.parent / "runs"))
