# Decisions

Why the agent is built the way it is. Each entry: what was decided, why, and what was turned down.

## 1. A self-hosted model; nothing leaves the machine

A payments system's logs and records are not sent to an outside AI service. The model runs on
this machine's GPU with llama.cpp's `llama-server` (Vulkan: the card is an AMD RX 9070 XT, 16 GB,
which Ollama's ROCm builds did not support), behind an OpenAI-compatible API, so another model or
server is a setting.
*Turned down:* a hosted model API.

## 2. A guided workflow, not a free agent loop

A 9–20B model is good at one well-posed question and poor at running a long open-ended loop of
tool calls. So code walks the investigation (a LangGraph graph) and the model makes only the
choices code cannot: which checks to run next, what the evidence means, and whether outside text
speaks to a reader. Every answer has a schema that allows only valid names, the gathering has a
hard limit (4 rounds of up to 3 checks), and code checks everything the model claims.
*Turned down:* a ReAct-style loop with free tool calls.

## 3. Named checks with fixed arguments

The runbook defines each check (`processor_pods` is `get_pod_status` of `processor-simulator`).
The model picks by name and never writes a query or an argument: a small model writes PromQL
badly, and fixed checks make results comparable across models and runs.
*Turned down:* a raw query tool.

## 4. Tools behind an MCP server, read-only, with judged results

Prometheus, Alertmanager, Kubernetes and PayFlow's API are reached through one MCP server. Each
tool returns a short result with `concerns` (what looks wrong, in plain words) rather than raw
metrics. Every tool only reads; PayFlow's API is reached as a client whose scopes cannot change
anything.

## 5. No vector database

The runbook is found by the alert's exact name. Similar-incident search may come later, and stays
only if the evals show it helps.

## 6. Every claim checked by code

The diagnosis must quote the checks, and code verifies each quote (part by part, against what the
model was shown). A cause must be quoted from a check that can show it and found something
(`shown_by`); some causes also need other checks to have run clean (`requires_clear`). A claim that
fails is explained to the model once; a cause still not backed becomes `unknown`.
*Why:* a model can draw a wrong conclusion from true facts, but it should never draw one from facts
no tool returned. The rules came from failures the evals found: a quote of "error_lines: 0" backing
an outage, and failed calls read as "unreachable" while the processor itself had failed.

## 7. Evidence is data, and outside text is neither an instruction nor a fact

Log lines and error texts can carry text written by outside parties. The prompt says so, and says
what a fact is: what the tools measured, and what a service logged about its own work. A
separate, narrow question after the diagnosis finds text that speaks to a reader, and the report
shows it to the operator as "found in the evidence, and not followed".
*Turned down:* matching instruction-like text with patterns (only finds the wordings someone
foresaw); a "set aside" field first in every answer (it made the diagnoses worse).

## 8. Graded by code, measured repeatably

Pass or fail is computed: the cause, the quotes, the key check, and for planted instructions,
what they got. No model judges a model. The model server's prompt cache is off by default: with
it on, the same prompts scored 12, 10 and 9 of 13. With it off a run repeats exactly within one
model-server process; between processes on different days the same prompts have scored up to two
scenarios apart, for no known reason, so comparisons are made within one process. Scenarios come
in sets: tuned on, held out, and sets frozen before any model ran them.

## 9. gpt-oss-20b as the default model

On the fresh set it tied with qwen3.5-9b (11 of 12 each, both ignoring every planted instruction)
and was 2.4 times faster; gemma-4-26b-a4b scored 10 of 12. The prompts were tuned on qwen3.5-9b.
Thinking is off, or as low as the model allows: the answer's own reasoning field does that job,
and hidden thinking did not work with schema-constrained answers on this server.

## 10. Traces as files, and record and replay

Each investigation is one JSON file, rewritten after every step, and its report is made from that
file alone. A recorded investigation can be replayed with another prompt or model, the tools
answering from the recording; the evals are built on this.
*Turned down:* a tracing service (Langfuse, LangSmith).

## 11. The service runs on this machine, beside the model

The model needs the GPU, which Docker Desktop cannot give a pod, so the agent runs on the host
too. It listens on 127.0.0.1: Docker Desktop's `host.docker.internal` reaches it from the cluster
anyway, so nothing is open to the network, and a bearer token guards it.
*Turned down:* running the agent in the cluster (for later, with a GPU node).

## 12. Every alert is sent; each new firing is investigated once

PayFlow's Alertmanager sends every alert to the agent, opt-in like its Slack receiver. Each new
firing starts one investigation; firings of an alert already being handled join it; repeats change
nothing; alerts without a runbook are recorded. One investigation runs at a time: there is one GPU.

## 13. A person approves; LangGraph pauses the workflow

The agent proposes the runbook's next step for the cause it found and waits. The pause is
LangGraph's own (`interrupt()`), with the state saved after every step in SQLite, so a decision
resumes the workflow even after a restart, and the coming steps (carry it out, check it worked)
follow the decision inside the same workflow. A proposal lapses when every alert behind it has
resolved. Decisions go through the service's API (and the command line, which calls it), so one
process owns the workflow's state. Approving a retry list carries it out (14 to 17); any other
approval records the decision, and the step is a person's.
*Turned down:* finishing the investigation and holding the proposal in the service (actions would
then need a second workflow).

## 14. One action: retrying parked sagas, with a second login used only after approval

The first thing the agent does in PayFlow is retry sagas parked for an operator
(`SagaRequiresAttention`): PayFlow already offers it as an operator's action, it is safe to repeat
(the step runs again under the same idempotency key), and it is what a person does after most
outages. It goes through a second PayFlow login, `incident-agent-operator`, whose scopes can read
and retry sagas and nothing else. Only the agent's service holds its secret; the MCP server, whose
tools the model chooses from, never does, not even from the `.env` file both read.
*Turned down:* giving the read-only login the retry scope (any tool bug could then change PayFlow);
an action tool in the MCP server (the model would sit one schema away from it).

## 15. Code decides which sagas may be retried, not the model

Each parked saga is sorted by the code PayFlow parked it for, the outcome of its last step: no
answer came (`CAPTURE_UNKNOWN`, `TRANSFER_UNKNOWN`, ...) goes on the retry list, at most 50; a
refusal by the processor, the bank or the ledger, a code the runbook does not know, or a saga that
could not be read needs a person, because the same request would be refused again. The model's
diagnosis explains the incident; it cannot add a saga to the list or drop one. A planted
"retry every saga" in the evidence changes nothing, and the evals check exactly that.
*Turned down:* letting the model pick the sagas (a list is a fact to look up, not a judgement);
retrying refusals too (noise at best).

## 16. Exactly what was approved, checked again, one at a time, then watched

Only the sagas on the approved list are retried, in order, one per workflow step, so the state
saved after each says exactly which are done and a restart never retries one twice. Just before
each: still parked for the same code, and its partner answering (its calls over the last 2
minutes all answered and the processor's breaker not open; with no calls, processor-simulator's
own health). A saga that changed, or whose partner does not answer yet, is skipped and reported.
Each retried saga is watched for up to 5 minutes until it finishes or parks again. An approval is
carried out in the background by a second worker: approving answers at once.
*Turned down:* retrying whatever is parked when the approval comes (not what the person saw);
all at once (a burst on a partner that just recovered); waiting for the partner instead of
skipping (an approval that acts an hour later is a different decision).

## 17. A half-open breaker is not a partner that is down

The first rule was "the processor's breaker closed on every instance". The live test showed that
after an outage with no traffic the breaker stays half-open indefinitely: it closes only after
enough trial calls succeed on each instance, even after three successful captures. So nothing
would ever be retried after a quiet-time outage. Now only an open breaker blocks; half-open goes on
to the calls and the health, and a retry is then one of the trial calls it is waiting for.
*Turned down:* keeping "closed only" and waiting for traffic; allowing half-open only after
answered trial calls (the same wait, without traffic).

## 18. A second action: running a failed day's reconciliation again

`ReconciliationRunFailed` is the third runbook, chosen next because PayFlow really had it: the
hourly out-of-memory crash of 2026-10-11 was found through it. Its action is the operator's
manual run (`POST /api/v1/reconciliation/runs`), through the same operator login with one more
scope, `reconciliation:admin`. A run reads and compares and moves no money, so running a day
again is always safe; the hourly catch-up would get to it within the hour, and only for the last
3 days. The action saves that hour and covers older days.
*Turned down:* leaving it to the catch-up (no action at all; the runbook would only explain).

## 19. Code sorts the failed days by the source their error names, and stops at three failures

As with the parked sagas, code makes the list: each day no run has reconciled, and whose newest
run failed, gets the cause of the host its error names (`ledger-service` in the URL that did not
answer), or "Abandoned" for a run whose pod died. All known causes may be run again, but a day
that has failed 3 times needs a person: on 2026-10-11 the processor came back after every crash,
and every run crashed it again. Before each rerun code checks the day is still not reconciled, no
run of it is going, and the source that failed is healthy again.
*Turned down:* rerunning whatever failed (it would have repeated the crash every hour); the model
choosing the days.

## 20. Code checks the diagnosis against its own sorting

Where code has sorted the sagas or days into causes, a diagnosed cause that none of them has is
not accepted: the model is told the causes code found and asked once more. Seen in the evals: a
model whose own reasoning said `partner_rejected` answered `processor_no_answer`, while no saga
had parked for lack of an answer. Sorting therefore runs before the diagnosis, not after it.
*Turned down:* replacing the model's cause with code's (the diagnosis would explain nothing code
had not already counted; the model still reads the logs and pods that say why).

## 21. Fixes found by a frozen set are tried on copies

The saga set was frozen and run once; its weaknesses (PayFlow's own "Saga needs attention." line
flagged as addressed to the operator) were fixed on tuning copies in `evals/scenarios/sagas-tuning`
(copies of the affected recordings plus a live recording no frozen set uses), and the frozen set
run once more after, reported as such. The first score stays on record.
*Turned down:* tuning on the frozen set (its score would no longer say how the agent does on what
it has not seen).

## 22. A check may name a label of the alert

`ServiceDown`, and alerts like it, are about whichever service fires them. Rather than one
runbook per service, a check's arguments may name the alert's label (`{workload: "{job}"}`); it
is filled in from the firing alert when the investigation starts, before the model sees the
checks. The model still only picks checks by name; it never writes an argument. A check whose
label no firing alert carries is left out, and the trace says so.
*Turned down:* letting the model fill in the service (an argument it writes is one it can get
wrong, or be talked into); one runbook per service (nine copies of one text).

## 23. Most alerts get no action

`ServiceDown`, `KafkaUnreachable` and `PayoutsFailing` propose a step for a person: scale a
service back up, fix what kills its container, bring Kafka back, contact merchants. The agent
does none of it. The plan kept restarts and scale-ups recommend-only from the start: an action
is added only where it is safe to repeat, checked before it runs, and narrow (retrying a parked
saga, running a reconciliation again).

## 24. What the live tests found about PayFlow's own alerts is reported, not changed

Two of PayFlow's alert rules miss a case, found while making the evals: `ServiceDown` never
fires for a service whose pods run but are never ready (Prometheus scrapes a pod whether or not
it is ready), and `KafkaUnreachable` never fires when kafka-exporter has no pods at all (its rule
is `up == 0`, with no `absent()`, unlike `ServiceDown`'s). The runbooks say so; PayFlow's rules
are left for a separate change.

A third, found when the ledger's database login was locked (2026-10-24): the ledger keeps running
and stays ready, but its metrics endpoint fails, because its outbox gauges query the database on
every scrape (each waits for a connection that never comes, and the error handler cannot write
its JSON answer to a metrics request). Prometheus then counts the ledger as down, so `ServiceDown`
and `InstanceDown` fire for a service whose pods are alive, and the agent's messaging check says
the ledger reports no outbox metrics. The fix belongs in PayFlow (gauges that read a value kept
up to date in the background, so a scrape never waits on the database).

Two of the three were then fixed in PayFlow, as a change of their own, not inside the evals
(2026-10-24): `KafkaUnreachable` has `or absent(up{job="kafka-exporter"})`, with a promtool test,
and the gauges of the ledger, payment-service and payout-service are refreshed every 10 seconds
in the background, and are unknown (NaN) while the database cannot be read. (The first version
kept the last values, and the end-to-end test caught it: one reply in flight when the database went
away aged into an `OutboxBacklog` for the ledger.) With the ledger's login locked for 3.5 minutes after the fix, Prometheus kept
seeing it up and nothing but the real symptom fired; the stalled-consumer recording was made again
on the fixed PayFlow. The unready-pod case is left as it is: a pod that runs but is not ready is
what `InstanceDown` and the service's own errors show.

## 25. A derived log line is one PayFlow could write

A derived recording changes what a check returned; when that is a log line, it is a line PayFlow
really logs: the class that logs it, at its level, in its wording, read from the service's code.
A planted instruction goes only where text from outside PayFlow really reaches a log: the
processor's answer, a merchant endpoint's answer, the mail provider's answer, a caller's request
path, an error's message. Found on 2026-10-24, before the last sets' first runs: lines from
classes that do not exist (`ChannelConfig`, payment-service's `OutboxRelay`), a "… note:" no class
writes, an outbox failure retried that PayFlow parks at once (`RecordTooLargeException`), and an
exception no consumer throws. Fixed in 12 recordings, the stalled consumer recorded live instead;
the earlier runs on them do not count.
*Turned down:* keeping them (a model can tell a line PayFlow would never write, and an eval of
that measures the wrong thing).

## 26. The log tool masks Java's identity hashes

A Java object printed without its own `toString` ends in `@` and a hash (`@1a2b3c4d`) that differs
for every object, so one error logged 1599 times was listed as 1599 kinds and pushed every other
line out of the tool's ten. The log tool now masks `@<hash>` like ids and numbers before grouping.
A recording made before the mask is regrouped the same way when something is derived from it.

## 27. A derived recording may be of a related alert

`ReconciliationNotRun` and `PayoutBatchNotRun` take a day or more to fire on their own. A failed
reconciliation run is what one of their causes looks like, so their recordings are derived from a
`ReconciliationRunFailed` one: the derivation names the other alert, rewrites the alert list, and
adds the results of the checks the other runbook has that the source never ran, each built with
the tool's own code. A rewrite may also change a firing alert's labels (a dead letter on
`payout-events.DLT` rather than `payment-created.DLT`).
*Turned down:* waiting a day per recording; leaving those two alerts without scenarios.

## 28. A quote may drop a sentence, and its typography is made plain

Two live investigations on 2026-10-24 named the right cause and were reported as "cause unknown",
because their quotes were refused: one wrote "ledger‑service" with a non-breaking hyphen
(U+2011), the other left the middle sentence out of a long log line, without "...", and was cut
at the 200-character limit. Quotes are now compared with typographic dashes, quotes and spaces
made plain, and are taken apart at the end of a sentence as well as at commas and "..."; every
part must still be found in the result of the check the quote names, and a part cut by the limit
is checked without its cut word.
*Turned down:* matching quotes loosely (by similarity): a made-up number or a sentence from
another check would then pass.

## 29. In the cluster: one image, two pods, the model on the host

The MCP server and the agent service run in PayFlow's cluster, in their own namespace, from one
image (two commands); the model stays on the host. Docker Desktop cannot give a container this AMD
GPU, and the model is one URL (`LLM_URL`), so a GPU node could take it later without other
changes. Each pod gets only what it needs: the MCP server a read-only Role in PayFlow's namespace
and the read-only client; the agent service no Kubernetes access and the operator login. The
service stays one replica with `Recreate` and a volume: its state is SQLite (investigations,
LangGraph's checkpoints, traces), which one process must own. The CLI keeps working unchanged
because the service is published on this machine's port 8092; reports now come from the service,
which keeps the traces (`show <n>`).
*Turned down:* two images (the same dependencies twice, for a few lines of difference); Postgres
for the agent's state (more to run for one replica; SQLite already survives restarts); the model
in a pod on the CPU (minutes per investigation instead of seconds).

## 30. One name for the agent, wherever it runs

PayFlow's Alertmanager sends to `incident-agent.payflow-agent.svc.cluster.local:8092` in every
case. Deployed, that is the agent's Service; for development, `k8s-deploy.sh host` makes it an
ExternalName for `host.docker.internal`, where `payflow-agent serve` runs. So PayFlow's overlays
do not change when the agent moves, and moving it is one command.
*Turned down:* a second PayFlow overlay per place the agent runs (two more overlays, with the
Slack ones four, to keep in step).

## 31. CI replays; the GPU scores

CI runs ruff and the tests, builds and smoke-tests the image, validates the manifests and checks
the shell scripts. The tests replay every scenario set with the scripted model and check every
derived recording against its rules, so a broken runbook, recording or derivation fails CI. The
real model's scores need the GPU, so they are made on the machine that has one and recorded in
`evals/results` and EVALS.md. The image and CI install the versions in `constraints.txt` (the
development environment's, taken with `pip freeze`), so all three run the same code.
*Turned down:* a GPU runner (cost, and the scores already repeat exactly on one machine);
unpinned dependencies in the image (a library release could change it between two builds).
