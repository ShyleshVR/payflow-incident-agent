"""Phase 6's alerts: checks filled in from the firing alert's labels (ServiceDown's {job}), and the
failed-payouts tool."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client

from agent import config, run, runbooks
from agent.trace import load
from mcp_server import server as mcp_server_module
from mcp_server.clients import payflow
from mcp_server.tools import alerts, payouts
from tests.fakes import FakeTools, ScriptedModel, alert

NOW = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)


# ---- labels in a check's arguments

def test_a_check_naming_a_label_is_filled_in_from_the_firing_alert():
    loaded = runbooks.load("ServiceDown", config.RUNBOOKS_DIR).as_dict()

    resolved = runbooks.resolve(loaded, [alert("ServiceDown", job="webhook-service")])

    checks = {check["name"]: check for check in resolved["checks"]}
    assert checks["service_pods"]["arguments"] == {"workload": "webhook-service"}
    assert "webhook-service's pods" in checks["service_pods"]["tells"]
    assert "Scale webhook-service back up" in next(c for c in resolved["causes"] if c["name"] == "scaled_to_zero")["proposal"]
    assert checks["database"]["arguments"] == {"workload": "postgres"}           # no label: as written
    assert resolved["unresolved_checks"] == []


def test_without_the_label_its_checks_are_left_out_and_from_the_rules_too():
    loaded = runbooks.load("ServiceDown", config.RUNBOOKS_DIR).as_dict()

    resolved = runbooks.resolve(loaded, [alert("ServiceDown")])

    assert resolved["unresolved_checks"] == ["service_errors", "service_health", "service_pods"]
    assert [check["name"] for check in resolved["checks"]] == ["database", "kafka"]
    assert next(c for c in resolved["causes"] if c["name"] == "crash_loop")["shown_by"] == []        # both need the label


def test_a_label_no_alert_carries_is_refused_when_the_runbook_is_read():
    text = (config.RUNBOOKS_DIR / "ServiceDown.md").read_text(encoding="utf-8").replace('{workload: "{job}"}', '{workload: "{service}"}', 1)
    with pytest.raises(runbooks.InvalidRunbook, match="names no label an alert carries"):
        runbooks.parse(text, "ServiceDown.md")


def test_an_investigation_runs_its_checks_with_the_alerts_label(tmp_path):
    tools = FakeTools([alert("ServiceDown", job="ledger-service")])
    model = ScriptedModel(["service_pods"])

    trace = asyncio.run(run.investigate("ServiceDown", tools, model, tmp_path, now=lambda: NOW))

    assert ("get_pod_status", {"workload": "ledger-service"}) in tools.asked
    assert load(trace.path)["runbook"]["checks"][0]["arguments"] == {"workload": "ledger-service"}


# ---- failed payouts

def payout(id_, status, code=None, minutes=10, account="ba_test_invalid", amount=25.0):
    at = (NOW.replace(tzinfo=None) - timedelta(minutes=minutes)).isoformat()
    return {"payoutId": id_, "status": status, "amount": amount, "currency": "USD", "bankAccount": account,
            "failureCode": code, "createdAt": at, "paidAt": at if status in ("PAID", "RETURNED") else None,
            "failedAt": at if status == "FAILED" else None, "returnedAt": at if status == "RETURNED" else None}


def test_failed_and_returned_payouts_of_the_window_are_grouped_by_code():
    listed = [payout("po_1", "FAILED", "invalid_account", 5), payout("po_2", "FAILED", "invalid_account", 7),
              payout("po_3", "RETURNED", "account_frozen", 20, account="ba_test_returned"),
              payout("po_4", "FAILED", "invalid_account", 90),          # before the window
              payout("po_5", "PAID", minutes=15, account="ba_test_ok")]

    result = payouts.summarise(listed, 1, NOW)

    assert (result.failed, result.returned, result.paid) == (2, 1, 1)
    assert [(g.status, g.failure_code, g.count) for g in result.groups] == [("FAILED", "invalid_account", 2),
                                                                           ("RETURNED", "account_frozen", 1)]
    assert result.groups[0].examples[0].id == "po_1"
    assert result.concerns[0] == "2 payout(s) failed in the last 1 hour(s) with invalid_account (the merchant's bank details)"


def test_over_mcp_it_reads_the_newest_hundred(monkeypatch):
    asked = []

    async def fake_get(path, params=None):
        asked.append((path, params))
        return [payout("po_1", "FAILED", "account_closed", 3, account="ba_test_closed")]
    monkeypatch.setattr(payflow, "get", fake_get)
    monkeypatch.setattr(alerts, "utc_now", lambda: NOW)

    async def call():
        async with Client(mcp_server_module.server) as client:
            return await client.call_tool("get_failed_payouts", {"hours": 500})

    result = asyncio.run(call())

    assert asked == [("/api/v1/payouts", {"limit": 100})]
    assert result.structured_content["hours"] == payouts.MAX_HOURS
    assert result.structured_content["groups"][0]["failure_code"] == "account_closed"


# ---- the frozen sets of phase 6's other alerts

SETS = {"service-down": ({"ServiceDown"}, 6), "kafka": ({"KafkaUnreachable"}, 6), "payouts": ({"PayoutsFailing"}, 5),
        "consumer-lag": ({"KafkaConsumerLag"}, 3), "saga-stuck": ({"SagaStepStuck"}, 3), "outbox": ({"OutboxBacklog"}, 3),
        "parked": ({"OutboxMessageParked"}, 3), "gateway": ({"GatewayErrorRate"}, 3), "instance": ({"InstanceDown"}, 3),
        "dead-letters": ({"DeadLettersArriving"}, 3),
        "discrepancies": ({"ReconciliationDiscrepancies"}, 3), "not-run": ({"ReconciliationNotRun", "PayoutBatchNotRun"}, 5)}


@pytest.mark.parametrize("name", sorted(SETS))
def test_each_set_replays_without_a_model_and_its_planted_text_is_where_it_says(name, tmp_path):
    from agent.tools import ReplayTools
    from evals import scenario
    alert_names, size = SETS[name]
    found = scenario.load_all(scenario.SCENARIOS_DIR / name)

    assert len(found) == size and {item.alert for item in found} == alert_names
    for item in found:
        trace = asyncio.run(run.investigate(item.alert, ReplayTools(load(item.recording)), ScriptedModel(),
                                            tmp_path / item.name, now=lambda: NOW))
        assert load(trace.path)["status"] == "completed", item.name
        if item.has_planted_instruction:
            recorded = load(item.recording)
            carried = [call["result"] for step in recorded["steps"] for call in step["tool_calls"]
                       if call["tool"] == "get_recent_errors" and not call["is_error"]]
            assert any("AI agent:" in str(result) or "Operator:" in str(result) for result in carried), item.name
