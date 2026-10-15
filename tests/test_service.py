import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from agent import run
from agent.llm import ModelUnavailable
from agent.run import NotFiring
from api import service as services
from api import store as states
from api.app import create_app
from api.service import Service
from api.store import Store
from tests.fakes import CIRCUIT_OPEN, FakeTools, ScriptedModel

OUTAGE = {"root_cause": "processor_outage", "confidence": "high", "summary": "The processor was switched off on its side.",
          "evidence": [{"check": "processor_errors", "quote": "Simulated outage set to 240s"}]}
ERRORS = {"workload": "processor-simulator", "error_lines": 0, "warning_lines": 1, "concerns": [],
          "groups": [{"level": "WARN", "logger": "c.s.p.web.ProcessorController", "message": "Simulated outage set to 240s", "count": 1}]}


def alert(fingerprint="f1", starts_at="2026-10-15T10:00:00Z", status="firing", name="ProcessorCircuitOpen", **labels):
    """One alert of an Alertmanager webhook notification."""
    return {"status": status, "labels": {"alertname": name, "severity": "critical"} | labels, "annotations": {},
            "startsAt": starts_at, "endsAt": "2026-10-15T10:20:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
            "fingerprint": fingerprint}


def notification(*alerts):
    return {"version": "4", "status": "firing", "receiver": "incident-agent", "groupKey": "{}:{alertname=\"x\"}", "alerts": list(alerts)}


class Done:
    """What run.investigate returns, as far as the service reads it."""

    def __init__(self, cause="processor_outage", trace_id="20261015T100100Z-ProcessorCircuitOpen"):
        self.document = {"id": trace_id, "result": {"diagnosis": {"root_cause": cause}}}


def make(outcomes=(), runbooks=("ProcessorCircuitOpen",), path=":memory:"):
    """A service whose investigations end as `outcomes` says, one per call (a Done, or an exception)."""
    outcomes, asked = list(outcomes), []

    async def investigate(alert_name):
        asked.append(alert_name)
        outcome = outcomes.pop(0) if outcomes else Done()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return Service(Store(path), investigate, lambda name: name in runbooks, retry_after=0.01), asked


def test_each_new_firing_starts_one_investigation_and_the_rest_of_the_notifications_change_nothing():
    service, _ = make()

    first = service.receive(notification(alert("f1")))
    again = service.receive(notification(alert("f1")))                                   # Alertmanager repeats it
    second_instance = service.receive(notification(alert("f2", application="payout-service")))   # another label set, same alert
    other = service.receive(notification(alert("f9", name="KafkaConsumerLag")))
    resolved = service.receive(notification(alert("f1", status="resolved")))

    assert first == {"investigations": [1], "joined": [], "repeats": 0, "resolved": 0, "no_runbook": [], "lapsing": []}
    assert (again["repeats"], again["investigations"]) == (1, [])
    assert second_instance["joined"] == [1]                                            # one incident, one investigation
    assert other["no_runbook"] == [2] and service.store.investigation(2)["status"] == states.NO_RUNBOOK
    assert resolved["resolved"] == 1
    firings = service.store.firings_of(1)
    assert [(f["fingerprint"], f["resolved_at"]) for f in firings] == [("f1", "2026-10-15T10:20:00Z"), ("f2", None)]
    assert firings[1]["labels"]["application"] == "payout-service"
    assert service.receive(notification({"status": "firing", "labels": {}})) == {
        "investigations": [], "joined": [], "repeats": 0, "resolved": 0, "no_runbook": [], "lapsing": []}   # nothing to go on: skipped


def test_investigations_run_one_at_a_time_oldest_first_and_record_how_they_ended():
    service, asked = make([Done(), NotFiring("ProcessorCircuitOpen", []), RuntimeError("boom")])
    for fingerprint in ("f1", "f2", "f3"):
        service.receive(notification(alert(fingerprint)))
        asyncio.run(service.process_one())    # each is done before the next firing arrives

    assert asked == ["ProcessorCircuitOpen"] * 3
    done, not_firing, failed = (service.store.investigation(i) for i in (1, 2, 3))
    assert (done["status"], done["root_cause"], done["trace_id"]) == ("completed", "processor_outage",
                                                                     "20261015T100100Z-ProcessorCircuitOpen")
    assert done["started_at"] and done["finished_at"]
    assert not_firing["status"] == "not_firing"
    assert (failed["status"], failed["error"]) == ("failed", "RuntimeError: boom")
    assert asyncio.run(service.process_one()) is None                                  # nothing left


def test_a_new_firing_after_the_last_investigation_ended_starts_a_new_one():
    service, _ = make()
    service.receive(notification(alert("f1", starts_at="2026-10-15T10:00:00Z")))
    asyncio.run(service.process_one())

    later = service.receive(notification(alert("f1", starts_at="2026-10-15T12:00:00Z")))   # it fired again

    assert later["investigations"] == [2]


class ConnectError(Exception):
    """Named like the HTTP library's: the MCP server refused the connection."""


@pytest.mark.parametrize("unreachable", [ModelUnavailable("the model server does not answer"), ConnectError("refused")])
def test_with_the_model_or_the_mcp_server_away_the_investigation_waits_in_the_queue(unreachable):
    service, asked = make([unreachable, Done()])
    service.receive(notification(alert()))

    with pytest.raises(services._Unreachable):
        asyncio.run(service.process_one())
    waiting = service.store.investigation(1)
    why = str(unreachable) if isinstance(unreachable, ModelUnavailable) else "the MCP server refused the connection (is it running?)"
    assert (waiting["status"], waiting["error"]) == ("queued", f"waiting: {why}")

    asyncio.run(service.process_one())
    assert service.store.investigation(1)["status"] == "completed" and len(asked) == 2


def test_the_worker_wakes_for_a_new_firing_and_tries_again_after_a_server_was_away():
    service, asked = make([ModelUnavailable("away"), Done()])

    async def scenario():
        worker = asyncio.create_task(service.run())
        await asyncio.sleep(0)
        service.receive(notification(alert()))
        for _ in range(200):
            if service.store.investigation(1)["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        worker.cancel()

    asyncio.run(scenario())

    assert service.store.investigation(1)["status"] == "completed" and len(asked) == 2


def test_nothing_is_forgotten_across_a_restart_and_a_running_investigation_is_queued_again(tmp_path):
    before, _ = make(path=tmp_path / "agent.sqlite")
    before.receive(notification(alert("f1")))
    before.store.update(1, status=states.RUNNING, started_at="2026-10-15T10:01:00Z")    # the service stops here

    after, asked = make(path=tmp_path / "agent.sqlite")

    assert after.store.requeue_running() == [1]
    assert (after.store.investigation(1)["status"], after.store.investigation(1)["started_at"]) == ("queued", None)
    assert after.receive(notification(alert("f1")))["repeats"] == 1                       # its firing is still known
    asyncio.run(after.process_one())
    assert after.store.investigation(1)["status"] == "completed" and asked == ["ProcessorCircuitOpen"]


# ---- over HTTP

def http_service(tmp_path):
    """A service whose investigations are real workflows, with a scripted model and fake tools."""
    async def investigate(alert_name):
        tools = FakeTools([CIRCUIT_OPEN], get_recent_errors=ERRORS)
        return await run.investigate(alert_name, tools, ScriptedModel(["processor_errors"], diagnoses=[OUTAGE]), tmp_path)

    return Service(Store(tmp_path / "agent.sqlite"), investigate, lambda name: name == "ProcessorCircuitOpen")


def wait_for(client, investigation_id, status, headers=None):
    for _ in range(300):
        found = client.get(f"/investigations/{investigation_id}", headers=headers).json()
        if found["status"] == status:
            return found
        time.sleep(0.01)
    raise AssertionError(f"investigation {investigation_id} is {found['status']}, not {status}")


def test_alertmanager_posts_its_alerts_and_the_investigation_and_its_report_can_be_read(tmp_path):
    with TestClient(create_app(http_service(tmp_path), tmp_path)) as client:
        posted = client.post("/alerts", json=notification(alert("f1", application="payment-service")))

        assert posted.status_code == 200 and posted.json()["investigations"] == [1]
        done = wait_for(client, 1, "completed")
        assert (done["alert_name"], done["root_cause"]) == ("ProcessorCircuitOpen", "processor_outage")
        assert done["firings"][0]["labels"]["application"] == "payment-service"
        assert done["report"].startswith("# ProcessorCircuitOpen: processor_outage")
        assert [item["id"] for item in client.get("/investigations").json()] == [1]
        assert client.get("/investigations/9").status_code == 404


def test_with_a_token_set_every_request_must_carry_it(tmp_path):
    with TestClient(create_app(http_service(tmp_path), tmp_path, token="s3cret")) as client:
        assert client.get("/investigations").status_code == 401
        assert client.get("/investigations", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/alerts", json=notification(alert())).status_code == 401
        assert client.get("/investigations", headers={"Authorization": "Bearer s3cret"}).status_code == 200
