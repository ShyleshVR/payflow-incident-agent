# Setting up and running

From a Windows machine with nothing installed to the agent investigating a real incident on its
own, then deciding on its proposal. Every step ends with a check, so a problem shows up where it
starts. Allow an hour, most of it downloads and PayFlow's first build.

What runs where when you are done:

| | Where | Port | Started by |
|---|---|---|---|
| PayFlow (services, Prometheus, Alertmanager) | Docker Desktop's Kubernetes, namespace `payflow` | 80, 9090, 9093 | `scripts/k8s-up.sh` in PayFlow |
| Model server (llama.cpp, gpt-oss-20b) | this machine, on the GPU | 8090 | `scripts\start-llm.ps1` |
| MCP server (the agent's read-only view of PayFlow) | the cluster, namespace `payflow-agent` | 8091 (inside the cluster) | `scripts/k8s-deploy.sh up` |
| Agent service | the cluster, namespace `payflow-agent` | 8092 (published on this machine) | `scripts/k8s-deploy.sh up` |

The model stays on this machine, where the GPU is: Docker Desktop cannot hand an AMD GPU to a
container. The pods reach it at `host.docker.internal`, which Docker Desktop routes to this
machine even though the model server listens on 127.0.0.1 only. For development the MCP server
and the agent service can run on this machine instead (step 5).

## What you need

| | |
|---|---|
| Windows 10 or 11 | the scripts are PowerShell and Git Bash |
| A GPU with 16 GB of video memory, supported by llama.cpp's Vulkan build | for gpt-oss-20b; smaller models need less (see step 2) |
| 32 GB of RAM | Docker Desktop takes about 16 GB for PayFlow |
| About 60 GB of disk | the model files and PayFlow's images |
| Docker Desktop, with Kubernetes enabled | *Settings → Kubernetes → Enable Kubernetes* |
| Git, with Git Bash | PayFlow's scripts and the agent's fault scripts are Bash |
| Java 21, Maven, `openssl` | to build PayFlow (openssl comes with Git Bash) |
| Python 3.12 or newer | for the agent |

The paths below assume the repositories in `D:\Payments-Engine` and `D:\payflow-incident-agent`
and the model files under `D:\llm`. Other places work: see the settings named at each step.

## 1. PayFlow, sending its alerts to the agent

In **Git Bash**:

```bash
git clone https://github.com/ShyleshVR/Payments-Engine.git /d/Payments-Engine
cd /d/Payments-Engine
git checkout feature/agent-phase-6               # the agent's access and alert routing; not on main yet

kubectl config use-context docker-desktop
scripts/generate-local-secrets.sh --agent       # first: makes the secrets, including the agent's
scripts/k8s-up.sh                               # builds the images and deploys (the first time, 10-20 minutes)
```

`--agent` creates `k8s/overlays/local/secrets/incident-agent-token`. Run it **before**
`k8s-up.sh`: that script chooses what to deploy from the files it finds, and with the token it
deploys the `local-agent` overlay, whose Alertmanager sends every alert to the agent too. Without
it, PayFlow runs as usual and the agent never hears of an alert. (Already deployed without it?
Run the `--agent` line now, then `SKIP_BUILD=1 scripts/k8s-up.sh`.)

**Check:**

```bash
kubectl -n payflow get pods            # everything Running (or Completed)
kubectl -n payflow exec deploy/alertmanager -- grep -c incident-agent /etc/alertmanager/alertmanager.yml
# 2: the agent's receiver is there
```

PayFlow's own documentation is in its `docs/DEPLOYMENT.md`.

## 2. The model server

1. **llama.cpp.** From [llama.cpp's releases](https://github.com/ggml-org/llama.cpp/releases),
   take the Windows Vulkan build (`llama-<build>-bin-win-vulkan-x64.zip`) and unzip it into
   `D:\llm\llama.cpp\<build>\`, so that `D:\llm\llama.cpp\<build>\llama-server.exe` exists. The
   start script expects build `b11429`; another one works with `-Build <build>`.
2. **The model.** Download `gpt-oss-20b-MXFP4.gguf` (12.1 GB) from
   [ggml-org/gpt-oss-20b-GGUF](https://huggingface.co/ggml-org/gpt-oss-20b-GGUF) into
   `D:\llm\models\`. (`LLM_HOME` changes `D:\llm`.)
3. **Start it**, in **PowerShell**, and leave the window open:

   ```powershell
   D:\payflow-incident-agent\scripts\start-llm.ps1
   ```

   It is ready when it prints `model loaded` and `listening on http://127.0.0.1:8090`.

**Check:** `curl.exe http://127.0.0.1:8090/health` answers `{"status":"ok"}`.

Other models, compared in [EVALS.md](EVALS.md#model-comparison):

| Model | File | Start with | Then set |
|---|---|---|---|
| qwen3.5-9b | `Qwen_Qwen3.5-9B-Q6_K.gguf` (bartowski/Qwen_Qwen3.5-9B-GGUF, 8 GB) | `-Model Qwen_Qwen3.5-9B-Q6_K.gguf -Alias qwen3.5-9b` | `LLM_MODEL=qwen3.5-9b` |
| gemma-4-26b-a4b | `google_gemma-4-26B-A4B-it-IQ4_XS.gguf` (bartowski/google_gemma-4-26B-A4B-it-GGUF, 14.2 GB) | `-Model google_gemma-4-26B-A4B-it-IQ4_XS.gguf -Alias gemma-4-26b-a4b` | `LLM_MODEL=gemma-4-26b-a4b` |

## 3. The agent

In **PowerShell**:

```powershell
git clone https://github.com/ShyleshVR/payflow-incident-agent.git D:\payflow-incident-agent
cd D:\payflow-incident-agent
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python scripts\write_env.py      # PayFlow's secrets for the agent, into .env
```

`write_env.py` copies three values from PayFlow's local secrets into `.env` (git-ignored) without
printing them: the agent's client secret for PayFlow's API (it can only read), the secret of its
second login (it can only retry parked sagas; the service uses it after an approval, the MCP
server never loads it), and the token PayFlow's alerts carry. Run it again whenever PayFlow's secrets are regenerated. (PayFlow
somewhere else: `write_env.py <path to PayFlow>`.)

**Check:**

```powershell
.venv\Scripts\python -m pytest -q                       # all pass; no GPU or cluster needed
Select-String -Path .env -Pattern '^[A-Z_]+=' | ForEach-Object { ($_.Line -split '=')[0] }
# PAYFLOW_CLIENT_SECRET, PAYFLOW_OPERATOR_CLIENT_SECRET and AGENT_TOKEN
```

Commands below are written as `payflow-agent`; that is `.venv\Scripts\payflow-agent.exe` (or run
`.venv\Scripts\Activate.ps1` once in the window).

## 4. The agent, in the cluster

In **Git Bash**, in `/d/payflow-incident-agent`:

```bash
scripts/k8s-deploy.sh up
```

It builds the agent's image (`deploy/Dockerfile`: the code and the runbooks, Python 3.13), makes
two Secrets from `.env` without printing them (the MCP server gets only its read-only client
secret; the agent service gets the alerts' token and its operator secret), and applies
`deploy/k8s`: the namespace `payflow-agent`, the MCP server with a read-only Role in PayFlow's
namespace, and the agent service with a 1 GiB volume for its database and traces, published on
this machine's port 8092. It waits until both run. Run it again after changing the code.

**Check:**

```powershell
payflow-agent pending
# Nothing waits for a decision.
```

```bash
scripts/k8s-deploy.sh status                       # two deployments, 1/1 each
kubectl -n payflow exec deploy/alertmanager -- wget -q -O /dev/null \
  http://incident-agent.payflow-agent.svc.cluster.local:8092/docs && echo reachable
```

From now on every alert PayFlow raises arrives here. Alerts the agent has no runbook for are
recorded (`no_runbook`), not investigated: every alert in PayFlow's rules has one, so this
happens only for an alert added to PayFlow after the runbooks were written.

## 5. Or: the agent on this machine (development)

To change the agent and try it at once, run its two programs here instead. PayFlow's Alertmanager
always sends to `incident-agent.payflow-agent.svc.cluster.local:8092`; `host` makes that name point
at this machine and scales the agent's pods to zero (their volume is kept):

```bash
scripts/k8s-deploy.sh host
```

Then, in two PowerShell windows in `D:\payflow-incident-agent`, left open:

```powershell
.venv\Scripts\python -m mcp_server.server
payflow-agent serve
```

The MCP server reads Prometheus (`localhost:9090`) and Alertmanager (`localhost:9093`), which
PayFlow's local overlay publishes on this machine; Kubernetes through your kubeconfig (context
`docker-desktop`); and PayFlow's API through its gateway (`http://localhost`). The service keeps
its state in `runs\`. `scripts/k8s-deploy.sh up` goes back to the cluster (stop the two first:
both want port 8092).

**Check:** `payflow-agent investigate ProcessorCircuitOpen` answers "ProcessorCircuitOpen is not
firing" (or investigates it, if it is), and `payflow-agent pending` answers.

## 6. Try it: break PayFlow and watch

The agent repository has scripts that break PayFlow in known ways (`evals/faults/`, see its
README). In **Git Bash**, in `/d/payflow-incident-agent`:

```bash
evals/faults/traffic.sh                          # 5 payments a second for 5 minutes
evals/faults/processor-outage.sh start 240       # the card processor answers 503 for 4 minutes
evals/faults/wait-for-alert.sh ProcessorCircuitOpen
```

The alert fires about 90 seconds after the outage begins. Within seconds Alertmanager sends it to
the agent, which investigates (about 10 seconds with gpt-oss-20b) and then waits for you. In
PowerShell:

```powershell
payflow-agent pending
#    1  ProcessorCircuitOpen: processor_outage  (since ...)
#       proposed: Contact the card processor about the outage on their side ...
#       report:   payflow-agent show 1
payflow-agent show 1                             # the report: cause, evidence, what was checked
payflow-agent approve 1 --note "processor team contacted"
# or: payflow-agent reject 1 --reason "..."
```

Approving this proposal records the decision in the trace and the report: the step (contacting
the processor) is yours to take. Leave it undecided instead, and it lapses when the alert
resolves, about a minute after the outage ends. The outage ends by itself; to end it early:
`evals/faults/processor-outage.sh stop`.

**The one thing the agent does itself: retrying parked payments.** Park three captures, then let
the agent retry them:

```bash
evals/faults/captures-parked.sh start              # they park about 10 minutes later
evals/faults/wait-for-alert.sh SagaRequiresAttention
```

```powershell
payflow-agent pending                              # SagaRequiresAttention: processor_no_answer, with the 3 to retry
payflow-agent approve 2 --note "processor is back"
payflow-agent show 2                               # "What was carried out": each saga, retried and how it ended
```

Approve about 5 minutes after the outage ends (about 16 minutes after `start`). Approved earlier,
the agent's check just before each retry finds the processor not answering yet and skips them
all, and says so. Rejected, they stay parked. Afterwards, `evals/faults/captures-parked.sh stop`
retries any still parked.

**The other thing it does: reconciling a failed day again.** Make a day's reconciliation fail
with the ledger down, then let the agent run it again:

```bash
evals/faults/reconciliation-source-down.sh start ledger-service      # yesterday's run fails; the alert fires at once
```

```powershell
payflow-agent pending                              # ReconciliationRunFailed: ledger_unavailable, with the day to run again
```

```bash
evals/faults/reconciliation-source-down.sh stop ledger-service       # the ledger is back
```

```powershell
payflow-agent approve 3 --note "ledger is back"
payflow-agent show 3                               # "What was carried out": the day run again, and how it ended
```

Approved while the ledger is still down, the agent's check before the rerun finds it not healthy
and skips the day. `ReconciliationRunFailed` keeps firing for an hour after a failure, so a second
failure within that hour starts no new investigation in the service; investigate it by hand
(`payflow-agent investigate ReconciliationRunFailed`).

Other faults: `processor-not-running.sh`, `processor-crash-loop.sh`,
`processor-database-access-lost.sh`, `processor-unreachable.sh`, each with `start` and `stop`.
Run the `stop` of whatever you started. Leave 16 minutes between faults: the checks look back 15
minutes, and the second investigation would also see the first fault.

## 7. Without the service

These run on this machine, with the MCP server here too (step 5's first window), and keep their
traces in `runs\`:

```powershell
payflow-agent investigate ProcessorCircuitOpen    # one investigation now (the alert must be firing)
payflow-agent list                                # past investigations on this machine
payflow-agent show <trace id>                     # a report (a number asks the service instead)
payflow-agent investigate --replay runs\<id>.json # the same evidence, asked again (only the model server needed)
payflow-agent eval                                # the exams (only the model server needed): see EVALS.md
```

## After a reboot

Nothing here starts by itself. In this order:

1. **Docker Desktop.** Start it; Kubernetes and PayFlow's pods come back by themselves after a
   few minutes (`kubectl -n payflow get pods`).
2. **Model server:** `scripts\start-llm.ps1`.
3. In the cluster, the agent's pods come back with PayFlow's; until the model server is up, an
   investigation waits and is retried every 30 seconds. On this machine instead (step 5): the
   MCP server and `payflow-agent serve`.

Investigations, decisions and proposals waiting for a decision are kept across restarts (in the
cluster on the agent's volume, `incident-agent-data`; on this machine in `runs\agent.sqlite` and
`runs\checkpoints.sqlite`). An alert that arrived while the service was down is sent again by
Alertmanager on its next repeat (every 4 hours while it fires).

To stop: Ctrl+C in each window. PayFlow: `scripts/k8s-down.sh` in PayFlow deletes it, data
included (the secrets are kept). The agent: `kubectl delete namespace payflow-agent` deletes it
with its volume (and `kubectl -n payflow delete role,rolebinding incident-agent-reader`).

## When something does not work

| What you see | Why | What to do |
|---|---|---|
| `kubectl`: "Unable to connect to the server" | Docker Desktop is not running (it does not start with Windows) | Start it and wait for Kubernetes |
| `start-llm.ps1`: "Not found: ...llama-server.exe" or "...gguf" | llama.cpp or the model is not where the script looks | Check `D:\llm\llama.cpp\b11429\` and `D:\llm\models\`, or pass `-Build`, `-Model`, or set `LLM_HOME` |
| The model server stops loading, or answers very slowly | Not enough video memory (Gemma 26B spills into system memory) | A smaller model, or `-Context 8192` |
| `payflow-agent pending`: "The agent service does not answer on port 8092" | The service is not running | `scripts/k8s-deploy.sh status`; or, on this machine, `payflow-agent serve` |
| `k8s-deploy.sh up` done, but port 8092 answers nothing or the old service | A `payflow-agent serve` on this machine holds the port | Stop it, then `kubectl -n payflow-agent delete service incident-agent` and `scripts/k8s-deploy.sh up` |
| Alertmanager's log: "no such host" for the incident-agent receiver | Neither the agent's pod nor the `host` name is deployed | `scripts/k8s-deploy.sh up` (or `host`) |
| The agent's pod: investigations stay `queued`, "The model server does not answer" | The model server is not running on this machine | `scripts\start-llm.ps1`; the pod reaches it at `host.docker.internal:8090` |
| An investigation stays `queued`, its error "waiting: the MCP server refused the connection" | The MCP server is not running | Start it; the service retries every 30 seconds |
| An investigation stays `queued`, its error "waiting: The model server does not answer" | The model server is not running | Start it; the service retries every 30 seconds |
| Nothing arrives at the service when an alert fires | PayFlow was deployed without the agent's token | In PayFlow: `scripts/generate-local-secrets.sh --agent`, then `SKIP_BUILD=1 scripts/k8s-up.sh`; in the agent: `write_env.py`, then `scripts/k8s-deploy.sh up` |
| Alertmanager's log: "401" for the incident-agent receiver | The agent's `.env` token differs from PayFlow's (secrets regenerated) | `.venv\Scripts\python scripts\write_env.py`, then `scripts/k8s-deploy.sh up` (or restart the service) |
| Alertmanager's log: "permission denied" reading `/etc/alertmanager/agent/token` | An older PayFlow branch without the pod's `fsGroup` | Update PayFlow (main, or `feature/agent-phase-6`) and redeploy |
| An investigation ends `not_firing` | The alert cleared before its turn in the queue | Nothing to do |
| `no_runbook` for an alert | Every alert in PayFlow's rules (16) has a runbook in `runbooks/`; this one is new to PayFlow | Expected: write its runbook (`runbooks/<Alert>.md`) |
| A day's rerun is skipped: "reconciled meanwhile" | The hourly catch-up (or someone) reconciled it between the proposal and the approval | Nothing to do |
| An approved retry list skips every saga: "the processor does not answer yet" | The processor's calls in the last 2 minutes went unanswered, or its breaker is open, or (with no calls) its health still shows recent 5xx | Wait a few minutes after the outage and run `evals/faults/captures-parked.sh stop`, or approve the next proposal |
| Every saga of an approved list: "Not carried out: ... No operator secret" | `.env` has no `PAYFLOW_OPERATOR_CLIENT_SECRET` (PayFlow older than the operator login, or `write_env.py` not run since) | Update PayFlow, run its `generate-local-secrets.sh`, then `write_env.py`, and restart the service |
| The same question gets different answers on different runs | `LLM_CACHE_PROMPT=on` | Leave it off (the default) when results must repeat |
| A fault script fails with `kubectl` paths mangled (`C:/Program Files/Git/...`) | Git Bash rewrites paths | The scripts set `MSYS_NO_PATHCONV=1`; do the same in your own commands |

Logs to look at:

```bash
kubectl -n payflow logs deploy/alertmanager --since=10m | grep -i incident-agent   # deliveries to the agent
kubectl -n payflow logs deploy/alert-sink --since=10m                            # every notification, as JSON lines
```

The service prints each request and each postponed investigation: in the cluster
`kubectl -n payflow-agent logs deploy/incident-agent` (and `deploy/mcp-server`), on this machine
in its window. Each investigation's full record: `payflow-agent show <n> --trace`.
