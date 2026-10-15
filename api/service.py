"""The agent as a service: it takes Alertmanager's notifications, and investigates what fires.

Each new firing starts one investigation; a firing of an alert whose investigation is still
queued or running joins it (one incident, one investigation, as on the command line).
Alertmanager's repeats of a firing change nothing, and its "resolved" notice is noted on the
firing. An alert with no runbook is recorded as such and not investigated.

Investigations run one at a time, oldest first: there is one model, on one graphics card. When
the model server or the MCP server cannot be reached, the investigation goes back in the queue
and is tried again a little later; any other failure ends it as failed, with the error kept.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from agent import runbooks
from agent.llm import ModelUnavailable
from agent.run import NotFiring, refused_connection
from agent.trace import Trace
from api import store as states
from api.store import Store

log = logging.getLogger(__name__)

# After the model or MCP server could not be reached, how long to wait before trying again.
RETRY_AFTER_SECONDS = 30.0


class Service:
    def __init__(self, store: Store, investigate: Callable[[str], Awaitable[Trace]], has_runbook: Callable[[str], bool],
                 retry_after: float = RETRY_AFTER_SECONDS):
        self.store = store
        self._investigate = investigate
        self._has_runbook = has_runbook
        self._retry_after = retry_after
        self._wake: asyncio.Event | None = None

    def receive(self, notification: dict) -> dict:
        """One Alertmanager webhook notification. Returns what it did, for the reply."""
        done = {"investigations": [], "joined": [], "repeats": 0, "resolved": 0, "no_runbook": []}
        for alert in notification.get("alerts", []):
            labels = alert.get("labels", {})
            name, fingerprint, starts_at = labels.get("alertname"), alert.get("fingerprint"), alert.get("startsAt")
            if not (name and fingerprint and starts_at):
                continue
            if alert.get("status") == "resolved":
                done["resolved"] += self.store.resolve(fingerprint, starts_at, alert.get("endsAt") or starts_at)
                continue
            if self.store.firing(fingerprint, starts_at):
                done["repeats"] += 1
                continue
            if not self._has_runbook(name):
                investigation_id = self.store.create(name, states.NO_RUNBOOK)
                done["no_runbook"].append(investigation_id)
            elif open_one := self.store.open_for(name):
                investigation_id = open_one["id"]
                done["joined"].append(investigation_id)
            else:
                investigation_id = self.store.create(name)
                done["investigations"].append(investigation_id)
            self.store.add_firing(fingerprint, starts_at, name, labels, investigation_id)
        if done["investigations"] and self._wake is not None:
            self._wake.set()
        return done

    async def process_one(self) -> int | None:
        """Runs the oldest queued investigation to its end. None when nothing is queued."""
        queued = self.store.next_queued()
        if queued is None:
            return None
        investigation_id = queued["id"]
        self.store.update(investigation_id, status=states.RUNNING, started_at=states.utc_now(), error=None)
        try:
            trace = await self._investigate(queued["alert_name"])
        except NotFiring as e:
            self.store.update(investigation_id, status=states.NOT_FIRING, finished_at=states.utc_now(), error=str(e))
        except Exception as e:  # every failure is this investigation's outcome
            if isinstance(e, ModelUnavailable) or refused_connection(e):
                # not the investigation's fault: back in the queue, tried again later
                why = str(e) if isinstance(e, ModelUnavailable) else "the MCP server refused the connection (is it running?)"
                self.store.update(investigation_id, status=states.QUEUED, started_at=None, error=f"waiting: {why}")
                raise _Unreachable from e
            log.exception("investigation %s failed", investigation_id)
            self.store.update(investigation_id, status=states.FAILED, finished_at=states.utc_now(),
                              error=f"{type(e).__name__}: {e}")
        else:
            document = trace.document
            self.store.update(investigation_id, status=states.COMPLETED, finished_at=states.utc_now(), trace_id=document["id"],
                              root_cause=document["result"]["diagnosis"]["root_cause"])
        return investigation_id

    async def run(self) -> None:
        """Works through the queue for as long as the service runs."""
        self._wake = asyncio.Event()
        requeued = self.store.requeue_running()
        if requeued:
            log.info("queued again after a restart: %s", requeued)
        while True:
            try:
                if await self.process_one() is not None:
                    continue
                timeout = None
            except _Unreachable as e:
                log.warning("investigation postponed: %s", e.__cause__)
                timeout = self._retry_after
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass


class _Unreachable(Exception):
    """The model server or the MCP server did not answer."""


def runbook_exists(directory) -> Callable[[str], bool]:
    def exists(alert_name: str) -> bool:
        try:
            runbooks.load(alert_name, directory)
        except runbooks.NoRunbook:
            return False
        return True
    return exists
