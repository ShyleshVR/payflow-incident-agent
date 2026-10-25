# One image for both of the agent's processes in the cluster (deploy/k8s):
#   the MCP server      python -m mcp_server.server       (read-only tools)
#   the agent service   payflow-agent serve               (Alertmanager's webhook, investigations, decisions)
# The model is not in it: it runs on the host's GPU (scripts/start-llm.ps1), reached at
# host.docker.internal.
#
#   docker build -f deploy/Dockerfile -t payflow-incident-agent:local .     (scripts/k8s-deploy.sh does this)
FROM python:3.13.5-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, at the versions the tests ran with, so a code change does not reinstall them.
COPY pyproject.toml constraints.txt ./
RUN mkdir -p agent mcp_server/clients mcp_server/tools api cli evals \
    && pip install -c constraints.txt -e . \
    && rm -rf payflow_incident_agent.egg-info

COPY agent/ agent/
COPY mcp_server/ mcp_server/
COPY api/ api/
COPY cli/ cli/
COPY evals/__init__.py evals/*.py evals/
COPY runbooks/ runbooks/
RUN pip install -c constraints.txt --no-deps -e . \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent

# The service's state (its database, the paused workflows, one trace per investigation) goes to
# /data, a volume; nothing is written anywhere else.
ENV RUNS_DIR=/data/runs \
    AGENT_DB=/data/agent.sqlite \
    AGENT_CHECKPOINTS=/data/checkpoints.sqlite

USER 10001
CMD ["payflow-agent", "serve", "--host", "0.0.0.0"]
