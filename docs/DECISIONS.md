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
it on, the same prompts scored 12, 10 and 9 of 13. Scenarios come in three sets: tuned on, held
out, and a fresh set frozen before any model ran it, for comparing models fairly.

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
process owns the workflow's state. Approving records the decision; the agent acts in a later phase.
*Turned down:* finishing the investigation and holding the proposal in the service (actions would
then need a second workflow).
