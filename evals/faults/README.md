# Causing an incident, to record it

Each script here breaks one thing in a local PayFlow cluster and puts it back. They are run by
hand, from Git Bash, to make the recordings in `evals/fixtures`. An eval never runs them: it
replays the recordings.

They need the PayFlow repository beside this one (`PAYFLOW_DIR`, default `/d/Payments-Engine`)
for its load test and its local secrets, `kubectl` pointed at the cluster, and Prometheus and
Alertmanager on their usual local ports.

| Script | What it breaks | Expected cause |
|---|---|---|
| `processor-outage.sh start [seconds]` | The processor answers every request with 503 (its own test hook) | `processor_outage` |
| `processor-not-running.sh start` | The processor is scaled to zero | `processor_not_running` |
| `processor-crash-loop.sh start` | The processor's pods crash as they start | `processor_not_running` |
| `processor-database-access-lost.sh start` | The processor's database login is locked | `processor_dependency_down` |
| `processor-unreachable.sh start` | payment-service is given a wrong address for the processor | `processor_unreachable` |

Every script takes `stop`, which undoes it. `traffic.sh` and `wait-for-alert.sh` are helpers.

## Recording a scenario

Start the model server (`scripts/start-llm.ps1`) and the MCP server
(`python -m mcp_server.server`), then:

```bash
evals/faults/traffic.sh                                # payments flow; no traffic, no alert
evals/faults/processor-crash-loop.sh start             # the fault
evals/faults/wait-for-alert.sh ProcessorCircuitOpen    # about 90 seconds
payflow-agent investigate ProcessorCircuitOpen         # the live investigation; its trace is the recording
cp "$(ls runs/*.json | tail -1)" evals/fixtures/ProcessorCircuitOpen-processor-crash-loop.json
evals/faults/processor-crash-loop.sh stop              # put it back
```

Then write `evals/scenarios/<name>.yaml`: what happened, how it was caused, the root cause a
correct investigation reports and the key checks, the ones whose results show that cause. Read
the recording before choosing them. `payflow-agent eval --scenario <name>` refuses a scenario
whose recording does not hold a result for every check of the runbook.

For "the outage has just ended", start the outage with a length (`start 150`), wait for the
alert, run `processor-outage.sh wait-ended` and investigate the moment it returns.

## Before the next one

Wait **at least 16 minutes after undoing a fault**, and until the traffic has stopped, no
payment is still being retried and ProcessorCircuitOpen no longer fires. The checks look back
15 minutes (logs, call counts), so a recording made sooner shows the previous fault as well: the
processor's log of a database fault five minutes earlier is enough to turn "unreachable" into
"dependency down". Other alerts can stay for up to an hour after a processor fault (PayoutsFailing,
DeadLettersArriving); they do not need to clear.

A recording is a real cluster at a real moment, so some leftovers stay in anyway (a pod that
restarted ten minutes ago, Kubernetes warnings, which are kept for 30 minutes); say so in the
scenario's `caused_by`.

`traffic.sh` uses PayFlow's load test, which reports FAIL while a fault runs (expected) and
writes two result files into PayFlow's `load-tests/results`; the script deletes them when the
traffic ends.

## Derived recordings

A situation not worth breaking a cluster for is derived from a live recording by
`python -m evals.derive`. It does one of two things to a check's result: replace it with the
error its tool returns when its source does not answer, or plant something in it (a log line, a
parked saga) that carries an instruction aimed at the agent. The second kind are the safety
scenarios (`planted-*`): their files say which check carries the instruction and what it must
not get. The rules are in `evals/derive.py`; a derived recording is never edited by hand, and a
test fails when the file and the rules disagree.
