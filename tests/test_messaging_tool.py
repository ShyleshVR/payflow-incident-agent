import asyncio

import httpx
from mcp import Client

from mcp_server import server as mcp_server_module
from mcp_server.clients import prometheus
from mcp_server.tools import messaging


def lag(group, topic, value):
    return ({"consumergroup": group, "topic": topic}, float(value))


def per(label, **values):
    return [({label: name.replace("_", "-")}, float(value)) for name, value in values.items()]


def one(value):
    return [({}, float(value))]


# What Prometheus answers on a quiet cluster (the shapes are the live ones).
QUIET = {
    "kafka_up": one(1), "kafka_up_before": one(1), "brokers": one(1),
    "lag": [lag("ledger-service", "ledger-commands", 0), lag("payment-service", "ledger-replies", 0),
            lag("webhook-service", "payment-created", 0)],
    "lag_before": [lag("ledger-service", "ledger-commands", 0), lag("payment-service", "ledger-replies", 0),
                   lag("webhook-service", "payment-created", 0)],
    "consumers": per("consumergroup", ledger_service=12, payment_service=6, webhook_service=12),
    "outbox_pending": per("application", ledger_service=0, payment_service=0, payout_service=0),
    "outbox_pending_before": per("application", ledger_service=0, payment_service=0, payout_service=0),
    "outbox_oldest_seconds": per("application", ledger_service=0, payment_service=0, payout_service=0),
    "outbox_parked": per("application", ledger_service=0, payment_service=0, payout_service=0),
    "dead_letters": [({"topic": "webhook-deliveries.DLT"}, 21.0), ({"topic": "payment-created.DLT"}, 0.0)],
    "dead_letters_before": [({"topic": "webhook-deliveries.DLT"}, 21.0), ({"topic": "payment-created.DLT"}, 0.0)],
}


def test_a_quiet_cluster_lists_every_consumer_and_outbox_and_has_no_concerns():
    result = messaging.assess(15, QUIET)

    assert result.model_dump() == {
        "minutes": 15, "kafka_reachable": True, "brokers": 1,
        "consumers": [
            {"group": "ledger-service", "topic": "ledger-commands", "lag": 0, "change": 0, "consumers": 12},
            {"group": "payment-service", "topic": "ledger-replies", "lag": 0, "change": 0, "consumers": 6},
            {"group": "webhook-service", "topic": "payment-created", "lag": 0, "change": 0, "consumers": 12},
        ],
        "outboxes": [
            {"service": "ledger-service", "pending": 0, "change": 0, "oldest_pending_seconds": 0, "parked": 0},
            {"service": "payment-service", "pending": 0, "change": 0, "oldest_pending_seconds": 0, "parked": 0},
            {"service": "payout-service", "pending": 0, "change": 0, "oldest_pending_seconds": 0, "parked": 0},
        ],
        # 21 old dead letters are history, not news
        "dead_letters": [],
        "concerns": [],
    }


def test_lag_over_the_alert_threshold_is_named_with_its_direction_and_listed_first():
    rising = messaging.assess(15, QUIET | {
        "lag": [lag("ledger-service", "ledger-commands", 0), lag("webhook-service", "payment-created", 800)],
        "lag_before": [lag("ledger-service", "ledger-commands", 0), lag("webhook-service", "payment-created", 200)],
    })
    falling = messaging.assess(30, QUIET | {
        "lag": [lag("webhook-service", "payment-created", 800)],
        "lag_before": [lag("webhook-service", "payment-created", 2000)],
    })

    assert [(row.group, row.lag, row.change) for row in rising.consumers] == [("webhook-service", 800, 600), ("ledger-service", 0, 0)]
    assert rising.concerns == [
        "webhook-service is 800 messages behind on payment-created (the alert fires above 500), up from 200 15 minutes ago"]
    assert falling.concerns == [
        "webhook-service is 800 messages behind on payment-created (the alert fires above 500), down from 2000 30 minutes ago"]


def test_lag_below_the_threshold_is_a_concern_only_when_it_is_large_enough_and_growing():
    def concerns(now, before):
        return messaging.assess(15, QUIET | {
            "lag": [lag("ledger-service", "ledger-commands", now)],
            "lag_before": [lag("ledger-service", "ledger-commands", before)],
        }).concerns

    assert concerns(300, 100) == ["ledger-service's lag on ledger-commands is growing: 100 to 300 in 15 minutes"]
    assert concerns(300, 400) == []   # draining
    assert concerns(40, 2) == []      # ordinary traffic
    assert concerns(500, 500) == []   # the alert fires above 500, not at it


def test_a_group_nobody_reads_for_is_named_even_before_its_lag_grows():
    result = messaging.assess(15, QUIET | {
        "consumers": per("consumergroup", ledger_service=0, payment_service=6, webhook_service=12),
        "lag": [lag("ledger-service", "ledger-commands", 30)],
    })

    assert result.consumers[0].consumers == 0
    assert result.concerns == ["ledger-service has no consumer connected: nothing is reading ledger-commands"]


def test_the_exporters_small_negative_lag_is_zero():
    result = messaging.assess(15, QUIET | {
        "lag": [lag("payout-service", "payout-ledger-replies", -5)],
        "lag_before": [lag("payout-service", "payout-ledger-replies", -5)],
    })

    assert (result.consumers[0].lag, result.consumers[0].change) == (0, 0)
    assert result.concerns == []


def test_an_outbox_that_is_not_draining_or_has_parked_messages_is_named():
    result = messaging.assess(15, QUIET | {
        "outbox_pending": per("application", ledger_service=0, payment_service=40, payout_service=3),
        "outbox_pending_before": per("application", ledger_service=0, payment_service=0, payout_service=3),
        "outbox_oldest_seconds": per("application", ledger_service=0, payment_service=600, payout_service=1),
        "outbox_parked": per("application", ledger_service=0, payment_service=0, payout_service=2),
    })

    assert result.outboxes[1].model_dump() == {
        "service": "payment-service", "pending": 40, "change": 40, "oldest_pending_seconds": 600, "parked": 0}
    assert result.concerns == [
        "payment-service's outbox is not draining: 40 pending, the oldest for 600 seconds, up from 0 15 minutes ago",
        "payout-service has 2 parked outbox message(s): each blocks the later messages of its payment",
    ]


def test_a_large_outbox_is_named_by_count_and_a_growing_one_before_it_alerts():
    def concerns(now, before, oldest=5):
        return messaging.assess(15, QUIET | {
            "outbox_pending": per("application", ledger_service=now, payment_service=0, payout_service=0),
            "outbox_pending_before": per("application", ledger_service=before, payment_service=0, payout_service=0),
            "outbox_oldest_seconds": per("application", ledger_service=oldest, payment_service=0, payout_service=0),
        }).concerns

    assert concerns(1500, 1500) == ["ledger-service's outbox is not draining: 1500 pending, the oldest for 5 seconds, unchanged over 15 minutes"]
    assert concerns(400, 150) == ["ledger-service's outbox is growing: 150 to 400 pending in 15 minutes"]
    assert concerns(400, 900) == []
    assert concerns(60, 0) == []


def test_a_service_without_outbox_metrics_is_reported_as_missing_not_left_out():
    result = messaging.assess(15, QUIET | {
        "outbox_pending": per("application", ledger_service=0, payout_service=0),
        "outbox_pending_before": per("application", ledger_service=0, payment_service=0, payout_service=0),
        "outbox_oldest_seconds": per("application", ledger_service=0, payout_service=0),
        "outbox_parked": per("application", ledger_service=0, payout_service=0),
    })

    assert result.outboxes[1].model_dump() == {
        "service": "payment-service", "pending": None, "change": None, "oldest_pending_seconds": None, "parked": None}
    assert result.concerns == ["payment-service reports no outbox metrics: it may be down"]


def test_dead_letters_are_the_arrivals_in_the_window_most_first():
    result = messaging.assess(10, QUIET | {
        "dead_letters": [({"topic": "webhook-deliveries.DLT"}, 25.0), ({"topic": "payment-created.DLT"}, 0.0),
                         ({"topic": "ledger-commands.DLT"}, 7.0)],
        "dead_letters_before": [({"topic": "webhook-deliveries.DLT"}, 21.0), ({"topic": "payment-created.DLT"}, 0.0)],
    })

    # ledger-commands.DLT did not exist 10 minutes ago: its first dead letters created it
    assert [(row.topic, row.arrived) for row in result.dead_letters] == [("ledger-commands.DLT", 7), ("webhook-deliveries.DLT", 4)]
    assert result.concerns == [
        "7 message(s) arrived in ledger-commands.DLT in the last 10 minutes: a consumer gave up on them",
        "4 message(s) arrived in webhook-deliveries.DLT in the last 10 minutes: a consumer gave up on them",
    ]


def test_old_dead_letters_are_not_news_when_there_is_no_history_to_compare_with():
    # Prometheus was not watching Kafka `minutes` ago (it restarted): nothing to subtract from
    result = messaging.assess(60, QUIET | {"kafka_up_before": [], "dead_letters_before": [], "lag_before": [],
                                            "outbox_pending_before": []})

    assert result.dead_letters == []
    assert [row.change for row in result.consumers] == [None, None, None]
    assert [row.change for row in result.outboxes] == [None, None, None]
    assert result.concerns == []


def test_kafka_out_of_reach_is_said_first_and_told_apart_from_no_data():
    down = messaging.assess(15, QUIET | {"kafka_up": one(0), "brokers": [], "lag": [], "lag_before": [], "consumers": [],
                                         "dead_letters": [], "dead_letters_before": []})
    unknown = messaging.assess(15, QUIET | {"kafka_up": [], "brokers": [], "lag": [], "consumers": [], "dead_letters": []})
    nobody = messaging.assess(15, QUIET | {"lag": [], "consumers": []})

    assert (down.kafka_reachable, down.brokers, down.consumers) == (False, None, [])
    assert down.concerns == ["Kafka cannot be reached (the exporter's last check failed): lag and dead letters may be missing below"]
    assert len(down.outboxes) == 3  # the outboxes come from the services, so they are still known
    assert unknown.kafka_reachable is None
    assert unknown.concerns == ["Prometheus has no data from the Kafka exporter: Kafka's state, lag and dead letters are unknown"]
    assert nobody.concerns == ["Kafka reports no consumer group: no service is reading any topic"]


def test_each_number_is_asked_for_now_and_for_the_start_of_the_window():
    queries = messaging.queries(45)

    assert set(queries) == set(QUIET)
    assert queries["lag"] == "sum by (consumergroup, topic) (kafka_consumergroup_lag)"
    assert queries["lag_before"] == "sum by (consumergroup, topic) (kafka_consumergroup_lag offset 45m)"
    assert queries["outbox_pending_before"] == "max by (application) (outbox_pending offset 45m)"
    assert queries["dead_letters_before"] == 'sum by (topic) (kafka_topic_partition_current_offset{topic=~".+[.]DLT"} offset 45m)'
    assert queries["kafka_up_before"] == 'max(up{job="kafka-exporter"} offset 45m)'


def test_labelled_rows_keep_every_series_and_drop_nan():
    answer = [{"metric": {"topic": "a"}, "value": [1791700000, "3"]}, {"metric": {"topic": "b"}, "value": [1791700000, "NaN"]}]

    async def rows(result):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}))
        async with httpx.AsyncClient(base_url="http://prometheus", transport=transport) as client:
            return await prometheus._rows(client, "anything")

    assert asyncio.run(rows(answer)) == [({"topic": "a"}, 3.0)]
    assert asyncio.run(rows([])) == []


def over_mcp(arguments, monkeypatch):
    asked = []

    async def fake_vectors(queries):
        asked.append(queries)
        return QUIET

    monkeypatch.setattr(prometheus, "vectors", fake_vectors)

    async def call():
        async with Client(mcp_server_module.server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            return tools["get_messaging_status"], await client.call_tool("get_messaging_status", arguments)

    return (*asyncio.run(call()), asked)


def test_the_tool_is_served_over_mcp_and_needs_no_arguments(monkeypatch):
    tool, result, asked = over_mcp({}, monkeypatch)

    assert tool.input_schema.get("required", []) == []
    assert result.structured_content["minutes"] == 15
    assert result.structured_content["kafka_reachable"] is True
    assert len(result.structured_content["consumers"]) == 3
    assert result.structured_content["concerns"] == []
    assert "offset 15m" in asked[0]["lag_before"]


def test_a_window_out_of_range_is_brought_into_range_not_refused(monkeypatch):
    _, result, asked = over_mcp({"minutes": 100000}, monkeypatch)

    assert not result.is_error
    assert result.structured_content["minutes"] == 120
    assert "offset 120m" in asked[0]["lag_before"]


def test_prometheus_not_answering_is_an_error_the_model_can_read(monkeypatch):
    async def unreachable(queries):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(prometheus, "vectors", unreachable)

    async def call():
        async with Client(mcp_server_module.server) as client:
            return await client.call_tool("get_messaging_status", {})

    result = asyncio.run(call())

    assert result.is_error
    assert "Prometheus did not answer" in result.content[0].text
