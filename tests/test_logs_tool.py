import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as K8s

from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import kube
from mcp_server.tools import alerts, logs

NOW = datetime(2026, 10, 11, 4, 30, tzinfo=UTC)

# Real lines from the cluster (ids kept as they were logged).
BLANK_TRACE = "[                                                 ]"
RETRY_1 = (f"2026-10-11T04:21:47.489Z  WARN 1 --- [webhook-service] [ebhook-worker-1] {BLANK_TRACE} "
           "c.s.w.s.impl.WebhookDeliveryServiceImpl  : Webhook delivery failed, will retry. "
           "deliveryId=adf66952-d2ef-47a1-b0d8-e2d1c63d6244, attempt=1, nextAttemptAt=2026-10-11T04:22:11.685235259, "
           "error=Connection failed: ConnectException")
RETRY_2 = (f"2026-10-11T04:22:12.431Z  WARN 1 --- [webhook-service] [ebhook-worker-3] {BLANK_TRACE} "
           "c.s.w.s.impl.WebhookDeliveryServiceImpl  : Webhook delivery failed, will retry. "
           "deliveryId=50533e02-9265-48ad-b9f9-4675e964a45e, attempt=2, nextAttemptAt=2026-10-11T04:23:20.469176155, "
           "error=Connection failed: ConnectException")
EXHAUSTED = (f"2026-10-11T04:28:57.642Z ERROR 1 --- [webhook-service] [ebhook-worker-6] {BLANK_TRACE} "
             "c.s.w.s.impl.WebhookDeliveryServiceImpl  : Webhook delivery failed (exhausted), queued for DLT. "
             "deliveryId=3d96edf0-cdd6-4aba-b2e7-bef30869ddb3, attempt=5, error=Connection failed: ConnectException")
INFO = (f"2026-10-11T04:29:22.638Z  INFO 1 --- [webhook-service] [   scheduling-3] {BLANK_TRACE} "
        "c.s.w.dlt.WebhookDeadLetterRelay         : Relayed 3 dead letters")
SPRINGDOC = (f"2026-10-11T04:20:25.641Z  WARN 1 --- [payment-service] [           main] {BLANK_TRACE} "
             "o.s.core.events.SpringDocAppInitializer  : SpringDoc /v3/api-docs endpoint is enabled by default. "
             "To disable it in production, set the property 'springdoc.api-docs.enabled=false'")
RECONCILIATION = '2026-10-11T04:21:27.024Z ERROR 1 --- [reconciliation-service] [   scheduling-1] [eb44a9cf1e2f9b8c3734e45ed8a5a0e5-0e786173138b020c] c.s.r.service.ReconciliationService      : Reconciliation of 2026-10-10 failed: org.springframework.web.client.ResourceAccessException: I/O error on GET request for "http://processor-simulator:8086/v1/reports/settlement": null\n\norg.springframework.web.client.ResourceAccessException: I/O error on GET request for "http://processor-simulator:8086/v1/reports/settlement": null\n\tat org.springframework.web.client.DefaultRestClient$DefaultRequestBodyUriSpec.exchange(DefaultRestClient.java:540) ~[spring-web-6.2.19.jar:6.2.19]\nCaused by: java.net.ConnectException\n\tat java.net.http/jdk.internal.net.http.HttpClientImpl.send(HttpClientImpl.java:951) ~[java.net.http:na]\nCaused by: java.nio.channels.ClosedChannelException\n\tat java.base/sun.nio.ch.SocketChannelImpl.ensureOpen(SocketChannelImpl.java:202) ~[na:na]'
KAFKA = "[2026-10-10 06:19:09,705] INFO [SnapshotEmitter id=1] Successfully wrote snapshot 00000000000000156946-0000000012 (org.apache.kafka.image.publisher.SnapshotEmitter)\n[2026-10-10 06:29:46,498] ERROR [GroupCoordinator id=1] Execution of FlushBatch failed due to Histogram recorded value cannot be negative.. (org.apache.kafka.coordinator.common.runtime.CoordinatorRuntime)\njava.lang.ArrayIndexOutOfBoundsException: Histogram recorded value cannot be negative.\n\tat org.apache.kafka.coordinator.common.runtime.HdrHistogram.record(HdrHistogram.java:100) ~[kafka-coordinator-common-4.0.0.jar:?]"
POSTGRES = '2026-10-11 04:21:27.019 UTC [261] FATAL:  connection to client lost\n2026-10-11 04:21:27.019 UTC [261] STATEMENT:  select a1_0.id,a1_0.amount from authorizations a1_0\n2026-10-11 04:24:11.047 UTC [27] LOG:  checkpoint starting: time\n2026-10-11 04:25:02.300 UTC [300] ERROR:  duplicate key value violates unique constraint "uq_ledger_transactions_event"\n2026-10-11 04:25:02.300 UTC [300] DETAIL:  Key (event_id)=(0b6f3c0e-58a5-4d1c-9a0e-2f4b7f9f5a11) already exists.\n2026-10-11 04:25:02.300 UTC [300] STATEMENT:  insert into ledger_transactions (id) values ($1)'


def pod_log(text, pod="webhook-service-a", crashed=False):
    return {"pod": pod, "crashed": crashed, "text": text}


# ---------------------------------------------------------------- reading the formats

def test_a_spring_log_gives_its_warnings_and_errors_and_skips_info():
    records = list(logs.parse("webhook-service", f"{RETRY_1}\n{INFO}\n{EXHAUSTED}"))

    assert [(r["level"], r["logger"]) for r in records] == [
        ("WARN", "c.s.w.s.impl.WebhookDeliveryServiceImpl"), ("ERROR", "c.s.w.s.impl.WebhookDeliveryServiceImpl")]
    assert records[0]["at"] == datetime(2026, 10, 11, 4, 21, 47, 489000, tzinfo=UTC)
    assert records[0]["message"].startswith("Webhook delivery failed, will retry. deliveryId=adf66952")
    assert records[0]["cause"] is None


def test_the_cause_of_a_stack_trace_is_its_deepest_caused_by():
    record = next(logs.parse("reconciliation-service", RECONCILIATION + "\n" + INFO))

    assert record["message"].startswith("Reconciliation of 2026-10-10 failed")
    assert record["cause"] == "java.nio.channels.ClosedChannelException"


def test_a_kafka_log_gives_the_logger_and_the_exception_when_nothing_caused_it():
    records = list(logs.parse("kafka", KAFKA))

    assert len(records) == 1
    assert records[0]["logger"] == "org.apache.kafka.coordinator.common.runtime.CoordinatorRuntime"
    assert records[0]["message"] == "[GroupCoordinator id=1] Execution of FlushBatch failed due to Histogram recorded value cannot be negative.."
    assert records[0]["cause"] == "java.lang.ArrayIndexOutOfBoundsException: Histogram recorded value cannot be negative."
    assert records[0]["at"] == datetime(2026, 10, 10, 6, 29, 46, 498000, tzinfo=UTC)


def test_a_postgres_log_gives_fatal_and_error_lines_with_their_detail():
    records = list(logs.parse("postgres", POSTGRES))

    assert [(r["level"], r["message"], r["cause"]) for r in records] == [
        ("ERROR", "connection to client lost", None),
        ("ERROR", 'duplicate key value violates unique constraint "uq_ledger_transactions_event"',
         "Key (event_id)=(0b6f3c0e-58a5-4d1c-9a0e-2f4b7f9f5a11) already exists."),
    ]


# ---------------------------------------------------------------- grouping

def test_ids_times_numbers_and_addresses_are_masked_so_repeats_compare_equal():
    message = lambda line: next(logs.parse("webhook-service", line))["message"]

    assert logs.template(message(RETRY_1)) == logs.template(message(RETRY_2)) == (
        "Webhook delivery failed, will retry. deliveryId=<id>, attempt=<n>, nextAttemptAt=<time>, "
        "error=Connection failed: ConnectException")
    assert logs.template("Payment pay_3f9a1c2b7d4e5f60 from 10.1.0.44:8080 took 250 ms") == "Payment <id> from <address> took <n> ms"
    assert logs.template(message(RETRY_1)) != logs.template(message(EXHAUSTED))


def test_repeats_become_one_group_with_a_count_errors_first():
    result = logs.summarise("webhook-service", 15, [
        pod_log(f"{RETRY_1}\n{RETRY_2}\n{EXHAUSTED}", pod="webhook-service-a"),
        pod_log(RETRY_2, pod="webhook-service-b"),
    ], NOW)

    assert (result.pods_read, result.error_lines, result.warning_lines) == (2, 1, 3)
    assert [(g.level, g.count, g.pods, g.last_seen_minutes_ago) for g in result.groups] == [("ERROR", 1, 1, 1), ("WARN", 3, 2, 8)]
    assert result.groups[1].message.startswith("Webhook delivery failed, will retry. deliveryId=adf66952")  # a real line
    assert result.concerns == ["1 ERROR line(s) of 1 kind(s) in the last 15 minutes"]


def test_known_harmless_warnings_and_lines_older_than_the_window_are_dropped():
    result = logs.summarise("webhook-service", 5, [pod_log(f"{SPRINGDOC}\n{RETRY_1}\n{EXHAUSTED}")], NOW)

    assert [g.level for g in result.groups] == ["ERROR"]  # the retry was 8 minutes ago; the window is 5
    assert logs.summarise("payment-service", 15, [pod_log(SPRINGDOC)], NOW).groups == []


def test_the_example_shown_is_one_that_has_a_cause():
    without_trace = RECONCILIATION.split("\n")[0].replace("04:21:27.024", "04:20:27.024")

    result = logs.summarise("reconciliation-service", 15, [pod_log(without_trace + "\n" + RECONCILIATION)], NOW)

    assert (result.groups[0].count, result.groups[0].cause) == (2, "java.nio.channels.ClosedChannelException")


def test_lines_from_a_container_that_died_are_marked():
    result = logs.summarise("webhook-service", 15, [pod_log(RETRY_1), pod_log(EXHAUSTED, crashed=True)], NOW)

    assert [(g.level, g.in_crashed_container) for g in result.groups] == [("ERROR", True), ("WARN", False)]
    assert result.concerns[1].startswith("a container died in this window")


def test_only_ten_kinds_are_listed_and_the_rest_are_counted():
    many = "\n".join(RETRY_1.replace("Webhook delivery failed", f"Failure kind {chr(65 + i)}") for i in range(14))

    result = logs.summarise("webhook-service", 15, [pod_log(many)], NOW)

    assert (len(result.groups), result.warning_lines) == (logs.MAX_GROUPS, 14)
    assert result.concerns == ["14 different kinds of line; only the first 10 are listed"]


def test_a_quiet_workload_has_nothing_to_report():
    result = logs.summarise("ledger-service", 15, [pod_log(INFO, pod="ledger-service-a")], NOW)

    assert (result.pods_read, result.error_lines, result.warning_lines, result.groups, result.concerns) == (1, 0, 0, [], [])


def test_the_window_is_kept_between_one_minute_and_two_hours():
    assert [logs.clamp_minutes(m) for m in (-5, 0, 1, 15, 120, 500)] == [1, 1, 1, 15, 120, 120]


# ---------------------------------------------------------------- which container instances to read

def test_the_instance_that_died_is_read_too_only_if_it_died_inside_the_window():
    def pod_that_last_exited(minutes_ago):
        terminated = None if minutes_ago is None else K8s(finished_at=NOW - timedelta(minutes=minutes_ago))
        return K8s(status=K8s(container_statuses=[K8s(last_state=K8s(terminated=terminated))]))

    window_start = NOW - timedelta(minutes=15)

    assert kube._instances_to_read(pod_that_last_exited(3), window_start) == [False, True]
    assert kube._instances_to_read(pod_that_last_exited(300), window_start) == [False]
    assert kube._instances_to_read(pod_that_last_exited(None), window_start) == [False]


# ---------------------------------------------------------------- the tool over MCP

def over_mcp(arguments, monkeypatch, fake_logs=lambda: [pod_log(f"{RETRY_1}\n{EXHAUSTED}")]):
    asked = []

    async def logs_(name, since):
        asked.append((name, since))
        return fake_logs()

    monkeypatch.setattr(kube, "logs", logs_)
    monkeypatch.setattr(alerts, "utc_now", lambda: NOW)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["get_recent_errors"], await client.call_tool("get_recent_errors", arguments)

    return (*asyncio.run(call()), asked)


def test_the_tool_is_served_over_mcp_and_reads_fifteen_minutes_by_default(monkeypatch):
    tool, result, asked = over_mcp({"workload": "webhook-service"}, monkeypatch)

    schema = str(tool.input_schema)
    assert "kafka" in schema and "postgres" in schema and "redis" not in schema
    assert asked == [("webhook-service", timedelta(minutes=15))]
    assert (result.structured_content["minutes"], result.structured_content["error_lines"]) == (15, 1)
    assert result.structured_content["groups"][0]["level"] == "ERROR"


def test_a_window_longer_than_two_hours_is_shortened_not_refused(monkeypatch):
    _, result, asked = over_mcp({"workload": "kafka", "minutes": 500}, monkeypatch, list)

    assert not result.is_error
    assert asked == [("kafka", timedelta(minutes=120))]
    assert result.structured_content["minutes"] == 120


def test_when_kubernetes_does_not_answer_the_tool_says_so(monkeypatch):
    def unavailable():
        raise kube.KubeUnavailable("HTTP 403 Forbidden")

    _, result, _ = over_mcp({"workload": "postgres"}, monkeypatch, unavailable)

    assert result.is_error
    assert "Kubernetes did not answer" in result.content[0].text


def test_a_java_objects_identity_hash_is_masked_so_one_error_stays_one_kind():
    lines = [f"Exception thrown when sending a message with key='{key}' and payload='com.shylesh.payment_service.event.EventEnvelope@{hash_}' to topic payment-created:"
             for key, hash_ in (("69ffb9aa-c601-4f9a-80bb-c55b83af82e0", "12004286"), ("0cb177ba-cd8b-4f0a-9c6f-b6b51b9a6bf9", "677c2876"))]
    assert logs.template(lines[0]) == logs.template(lines[1])
