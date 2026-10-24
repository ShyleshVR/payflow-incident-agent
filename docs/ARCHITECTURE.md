# Architecture

## The pieces

| Piece | Runs | What it does |
|---|---|---|
| **Model server** | llama.cpp's `llama-server`, Vulkan, on this machine's GPU (`scripts/start-llm.ps1`) | Answers the agent's questions. OpenAI-compatible API on `127.0.0.1:8090`. gpt-oss-20b by default |
| **MCP server** | `python -m mcp_server.server`, `127.0.0.1:8091` | The agent's view of PayFlow: twelve read-only tools |
| **Agent service** | `payflow-agent serve`, `127.0.0.1:8092` | Takes Alertmanager's alerts, queues investigations, waits for decisions, carries out approved saga retries and reconciliation reruns |
| **Workflow** | in the service (or `payflow-agent investigate`) | One investigation: a LangGraph graph whose steps are mostly code, with the model making two kinds of choices |
| **PayFlow** | Docker Desktop's Kubernetes | Sends every alert to the agent (the `local-agent` overlay), and answers its checks |

Everything runs on one machine. Docker Desktop lets pods reach a program on the host at
`host.docker.internal` even when it listens only on 127.0.0.1, so none of the agent's ports is
open to the network.

## The tools

The MCP server turns raw sources into short, judged results: each result carries `concerns`,
what looks wrong in it, in plain words. A small model reads a page of facts well and a dump of
metrics badly.

| Tool | From | Tells |
|---|---|---|
| `list_active_alerts` | Alertmanager | what is firing, since when |
| `get_service_health(service)` | Prometheus | instances up, 5xx share, response time, database connections waiting |
| `get_pod_status(workload)` | Kubernetes | pods wanted and ready, crash loops, last exit, warning events (the services, postgres, kafka, redis, kafka-exporter) |
| `get_recent_errors(workload, minutes)` | Kubernetes logs | WARN and ERROR lines, grouped, with their causes |
| `get_dependency_status(minutes)` | Prometheus | calls to the card processor and the bank, the circuit breaker |
| `get_messaging_status(minutes)` | Prometheus | Kafka consumer lag, outboxes, dead letters |
| `list_sagas_needing_attention`, `get_saga(id)` | PayFlow's API | stuck and parked payments and payouts |
| `list_parked_sagas(service)` | PayFlow's API | every parked saga of one service (up to 50), each with the code it parked for. For the agent's code, not a runbook check |
| `get_failed_payouts(hours)` | PayFlow's API | the payouts that failed or came back from the bank in the window, grouped by failure code, with examples and how many were paid |
| `list_failed_reconciliations` | PayFlow's API | every day of the last week (today included) that no run has reconciled and whose newest run failed, with its failed runs and the last one's error. For the agent's code, not a runbook check |
| `get_reconciliation(date)` | PayFlow's API | the daily reconciliation's runs and discrepancies |

PayFlow's API is reached as the service client `incident-agent`, whose scopes can only read.
The MCP server never holds the agent's other login (see "Carrying out an approval"): it skips
that secret in the `.env` file both share, and drops it if it inherits it.
A source that does not answer becomes the tool's error ("Prometheus did not answer ... This fact
is unavailable; it says nothing about PayFlow itself"), never an exception the model cannot see.

## The runbook

One file per alert, `runbooks/<Alert>.md`: a YAML header the code reads, and guidance in Markdown
the model reads.

- **causes**: the possible causes, each a name and a line saying what it means. The diagnosis
  names one of these, or `unknown`.
- **checks**: named tool calls with fixed arguments (`processor_pods` is `get_pod_status` of
  `processor-simulator`). The model picks checks by name; it never writes a query or an argument.
- **shown_by**: for each cause, the checks that can show it. A diagnosis of that cause must quote
  one of them, and the check must have found something (a log line, or a concern).
- **requires_clear**: checks that must have run and found nothing for a cause to stand.
  "The processor cannot be reached" means something only while the processor itself looks fine.
- **proposals**: for each cause, the next step proposed to a person.
- **parking_codes** and **retryable** (only in `SagaRequiresAttention.md`): which codes PayFlow
  parks a saga for mean which cause (`CAPTURE_UNKNOWN` is `processor_no_answer`), and which causes
  the agent may retry: only the two where no answer came, so a retry asks again under the same
  idempotency key. A refusal by a partner or the ledger needs a person.
- **error_sources** and **rerunnable** (only in `ReconciliationRunFailed.md`): which text in a
  failed run's error names which source (the host `ledger-service` in the URL that did not answer
  is `ledger_unavailable`; "Abandoned" is `run_abandoned`), and which causes' days the agent may
  run again: all of them, since a run reads and compares and moves no money. A day that has
  failed 3 times needs a person whatever its cause (something keeps breaking it).
- The guidance ends with **"What resolves it"**, which the report quotes.

A check's arguments (and its line, and a proposal) may name a label of the firing alert, such as
`{job}`: `ServiceDown` is about whichever service its `job` label names, so its
`service_pods` check is `get_pod_status` of `{job}`. The labels are filled in when the
investigation starts (`runbooks.resolve`), so the model still never writes an argument; a check
whose label no firing alert has is left out, and the trace says so.

Every one of PayFlow's 16 alert rules has a runbook:

| Runbook | About | Action |
|---|---|---|
| `ProcessorCircuitOpen` | the card processor cannot be reached or fails | none (recommend) |
| `SagaRequiresAttention` | payments or payouts parked for an operator | retry the sagas no answer came for |
| `ReconciliationRunFailed` | a day's reconciliation could not read one of its sources | run the failed days again |
| `ServiceDown` | a service has no healthy instance: scaled to zero, crash loop, unschedulable | none (recommend) |
| `KafkaUnreachable` | the broker down or failing, or only kafka-exporter down | none (recommend) |
| `PayoutsFailing` | payouts declined (merchant details), returned by the bank, or short of balance | none (recommend) |
| `KafkaConsumerLag` | a consumer group falling behind: not running, stalled on errors, a burst, offsets left behind | none (recommend) |
| `SagaStepStuck` | a saga step running too long: the ledger not answering, Kafka down, replies not read | none (recommend) |
| `OutboxBacklog` | a service's outbox not draining: Kafka unreachable, the relay failing, a burst | none (recommend) |
| `OutboxMessageParked` | a message that can never be published: too large, or not serializable | none (recommend) |
| `GatewayErrorRate` | the gateway answering with 5xx: the upstream down or failing, the gateway itself | none (recommend) |
| `InstanceDown` | one pod down while the others run: crashing, or hanging | none (recommend) |
| `DeadLettersArriving` | messages given up on: unreadable, or failing every time they are handled | none (recommend) |
| `ReconciliationDiscrepancies` | a reconciled day that does not add up: money not booked, booked without money, amounts, status, holds | none (recommend) |
| `ReconciliationNotRun`, `PayoutBatchNotRun` | a daily job that did not happen: the service not running, the job failing or not finishing | none (recommend) |

Restarts, scale-ups and contacting merchants stay recommend-only: a person approves the step and
takes it.

## An investigation

```
intake → load_runbook → choose_checks ⇄ gather → record_unchosen → sort_sagas → sort_days → diagnose → screen → propose → report → decide
                                                    (an approved retry list) → retry ⇄ retry → watch
                                                    (an approved rerun list) → rerun ⇄ rerun → watch_runs
```

| Step | Who | What |
|---|---|---|
| `intake` | code | Is the alert firing? Every firing instance of it is one incident |
| `load_runbook` | code | The alert's runbook, by exact name. No runbook, no investigation |
| `choose_checks` | **model** | Which checks next (up to 3), or "enough". The answer's schema lists only the checks not yet run |
| `gather` | code | Runs them through the MCP server. Back to `choose_checks`, at most 4 rounds |
| `record_unchosen` | code | Runs the checks the model did not choose, for the trace only, so a replay with another model can choose them |
| `sort_sagas`, `sort_days` | code | Only for a runbook with parking codes or error sources: reads every parked saga of the services the alert is about (or every failed day of the week) and sorts each into a cause by what PayFlow recorded: the action list (sagas to retry, at most 50; days to run again) and those that need a person |
| `diagnose` | **model** | The cause, a confidence, quotes from the checks, a summary. Code then checks every quote against what the checks really returned, the runbook's `shown_by` and `requires_clear`, and, where code sorted sagas or days, that the cause is one of theirs; a failure is explained to the model and it answers once more. A cause still not backed is reported as `unknown` |
| `screen` | **model** | One narrow question about only the text outside parties wrote (log lines, error causes, a saga's last error): does any of it speak to an agent or an operator? What it quotes is shown in the report as "found in the evidence, and not followed" |
| `propose` | code | The runbook's proposal for the cause found (none for `unknown`), with code's list when there is one. The model's diagnosis cannot add a saga or a day to the list, or drop one |
| `report` | code | The result, written as the report |
| `decide` | person | In the service only: the workflow pauses until a decision |
| `retry` | code | After an approved retry list, one saga per step: checked again, then retried with the operator login (see below) |
| `watch` | code | Each retried saga, every 10 seconds for up to 5 minutes, until it finishes or parks again |
| `rerun` | code | After an approved rerun list, one day per step: checked again, then its reconciliation run again with the operator login |
| `watch_runs` | code | A rerun PayFlow had not finished within its 60-second answer, every 15 seconds for up to 10 minutes |

The model never sees an instruction it could follow from the evidence: the prompt marks the
evidence as data written partly by outside parties, the answer's shape allows only the runbook's
names, and code checks everything the model claims. The model's two jobs are the ones code cannot
do: deciding what to look at, and reading what came back.

Every step is written to `runs/<id>.json` as it ends: the step, its output, every tool call with
its result, every question to the model with its answer, time and tokens. The report
(`runs/<id>.md`) is made from that file alone, so any past investigation's report can be made
again. A recorded investigation can be replayed with another prompt or model (`--replay`): the
tools answer from the recording, the model is asked afresh. The evals are built on this.

## The service

`payflow-agent serve` is a FastAPI application with two workers: one runs investigations, the
other carries out approvals.

- **Alerts in.** Alertmanager posts each group of alerts to `POST /alerts`. Each new firing (an
  alert instance and when it started) starts one investigation; a firing of an alert whose
  investigation is still queued, running or waiting for a decision joins it; repeats change
  nothing; a "resolved" notice is recorded on its firing. An alert without a runbook is recorded
  as `no_runbook`.
- **One at a time.** The worker runs the oldest queued investigation. There is one GPU. When the
  model server or the MCP server does not answer, the investigation goes back in the queue and is
  tried again 30 seconds later; any other failure ends it as `failed`, with the error kept.
- **Records.** `runs/agent.sqlite`: investigations and their firings. An investigation running
  when the service stopped is queued again when it starts, and an approval it was carrying out
  is carried on.
- **Security.** Every request needs `Authorization: Bearer <AGENT_TOKEN>`. Alertmanager sends the
  same token, from a Kubernetes secret mounted into it.

### Approval

When an investigation reaches a proposal, LangGraph's `interrupt()` pauses the workflow. Its
state is saved after every step in `runs/checkpoints.sqlite`, under the investigation's id, so the
pause survives a restart of the service. The investigation is `awaiting_decision`, its report
already written.

A decision (`POST /investigations/{id}/decision`, or `payflow-agent approve` / `reject`) resumes
the workflow at the `decide` step, which records who decided, when and why, in the trace and the
report. A rejection needs a reason. If every firing behind a waiting proposal resolves first, the
service resumes it as `lapsed`.

All three outcomes were seen live on PayFlow's cluster on 2026-10-15: an outage whose proposal
was approved from the command line after the service had been restarted while it waited; a
crash-looping processor (`processor_not_running`) whose proposal was rejected; and a two-minute
outage left undecided, which lapsed four seconds after its alarm resolved.

A rejection or a lapse is recorded at once. An approval is answered at once too: the
investigation becomes `carrying_out`, and the second worker resumes the workflow. For a proposal
without a retry list (every `ProcessorCircuitOpen` proposal), approving only records the
decision: the step is for a person to take.

### Carrying out an approval

The agent takes two actions, and only on a list a person approved: it retries parked sagas, and
it runs a failed day's reconciliation again. It has a second PayFlow login for them,
`incident-agent-operator` (scopes `payments:operate`, `payouts:operate` and
`reconciliation:admin`: it can read and retry sagas, and read and start reconciliation runs, and
nothing else). Only the service holds its secret, and only `agent/operator.py` uses it.

The sagas are taken one at a time, one workflow step each. Just before each one, code checks
again (`agent/carry_out.py`):

- **The saga**, read with the operator login: still parked, for the same code as when proposed.
- **Its partner answers now.** The processor's or bank's calls over the last 2 minutes were all
  answered, and the processor's circuit breaker is not open. If nobody called the partner,
  processor-simulator's own health decides (its pods ready, no 5xx). A half-open breaker does not
  stop a retry: without traffic it stays half-open after an outage indefinitely (seen live), and a
  retry is then one of its trial calls.

A saga that fails either check is skipped, with the reason; the others go on. A retried one
resumes the step it parked in under the same idempotency key, so nothing is charged, refunded or
paid out twice. Then each retried saga is watched until it finishes or parks again, for up to 5
minutes. The report's "What was carried out" says, per saga: retried and how it ended, skipped
and why, refused by PayFlow (it answers 409 to a saga that is not parked), or not carried out
(PayFlow did not answer).

The state is saved after every step, so a restart in the middle goes on with the next saga and
never retries one twice; the decision is not taken again.

Seen live on 2026-10-22, three times: an approval given at once, while the processor's breaker
was still half-open with unanswered calls (all three sagas skipped); an approval once the
processor answered, with the service stopped five seconds later and started again (it carried on
by itself; all three retried and completed within a second; the alert resolved); and a rejection
(nothing retried, the alert kept firing).

**A failed day's reconciliation** is carried out the same way, one day per step. Just before each:
the day is still not reconciled (no run of it completed meanwhile, none is going), and the source
its error named is healthy now (its pods all ready, no 5xx). Then `POST
/api/v1/reconciliation/runs`: PayFlow reconciles inside that request, seconds for an ordinary day;
an answer that has not come within 60 seconds means the run is still going, and `watch_runs`
follows it. The report says, per day, how the run ended (completed, with its discrepancies, or
failed again) or why it was not run. Seen live on 2026-10-23: with the ledger scaled to zero a
day's run failed; the service investigated (`ledger_unavailable`) and proposed that day; approved
once the ledger was back, the day was run again and completed clean.

## PayFlow's side

In PayFlow, `scripts/generate-local-secrets.sh --agent` creates
`k8s/overlays/local/secrets/incident-agent-token`. With it, `scripts/k8s-up.sh` deploys the
`local-agent` overlay (`local-slack-agent` with Slack too): Alertmanager gets a second receiver
that posts every alert to `http://host.docker.internal:8092/alerts` with the token, while still
sending to its in-cluster alert-sink. Without the token, PayFlow is unchanged.

PayFlow's payment-service logs every processor call that gets no answer it can trust, and every
change of its circuit breaker. That was added for the agent: without it, "payment-service cannot
reach the processor" left no trace that could show it. The line says why, in words, before the
exceptions it came from: "the processor's host name could not be resolved", "the connection was
refused", "no connection within the connect timeout", "no answer within the timeout". (Java reports
the first two alike, as "ConnectException: null"; the difference is in the deepest cause.)

The ledger logs a posting it refuses at WARN ("Ledger command refused ... reason=..."), so that its
log shows why a saga parked on it: it was logged at INFO, which no WARN-and-ERROR reader sees.

## What is kept where

| | |
|---|---|
| `runs/<id>.json`, `runs/<id>.md` | every investigation's trace and report |
| `runs/agent.sqlite` | the service's investigations, firings, decisions |
| `runs/checkpoints.sqlite` | paused workflows |
| `runs/evals/` | the traces of eval replays |
| `.env` | `PAYFLOW_CLIENT_SECRET`, `PAYFLOW_OPERATOR_CLIENT_SECRET` (never loaded by the MCP server), `AGENT_TOKEN` |

All of these are git-ignored. `evals/results/` (the scores) is kept in git.
