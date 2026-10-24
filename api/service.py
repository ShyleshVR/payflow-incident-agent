"""The agent as a service: it takes Alertmanager's notifications, and investigates what fires.

Each new firing starts one investigation; a firing of an alert whose investigation is still
queued or running joins it (one incident, one investigation, as on the command line).
Alertmanager's repeats of a firing change nothing, and its "resolved" notice is noted on the
firing. An alert with no runbook is recorded as such and not investigated.

Investigations run one at a time, oldest first: there is one model, on one graphics card. When
the model server or the MCP server cannot be reached, the investigation goes back in the queue
and is tried again a little later; any other failure ends it as failed, with the error kept.

An investigation that reaches a proposal waits for a person's decision (decide): approved or
rejected, which resumes its paused workflow. When every firing behind it has resolved before
anyone decided, it lapses: the workflow is resumed with that instead.

A rejection or a lapse is recorded at once. An approval is carried out in the background, by a
second worker beside the investigations (a retry list takes minutes, with its watch): the
decision is answered at once, the investigation is carrying_out, then completed. One that a
restart cut short is carried on from where it stopped.
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
                 retry_after: float = RETRY_AFTER_SECONDS, resume: Callable[[str, dict], Awaitable[Trace]] | None = None):
        """investigate(alert name) runs an investigation; resume(trace id, decision) finishes one
        that waits for a decision."""
        self.store = store
        self._investigate = investigate
        self._resume = resume
        self._has_runbook = has_runbook
        self._retry_after = retry_after
        self._wake: asyncio.Event | None = None
        self._wake_carrying_out: asyncio.Event | None = None
        self._deciding = asyncio.Lock()

    def receive(self, notification: dict) -> dict:
        """One Alertmanager webhook notification. Returns what it did, for the reply."""
        done = {"investigations": [], "joined": [], "repeats": 0, "resolved": 0, "no_runbook": [], "lapsing": []}
        for alert in notification.get("alerts", []):
            labels = alert.get("labels", {})
            name, fingerprint, starts_at = labels.get("alertname"), alert.get("fingerprint"), alert.get("startsAt")
            if not (name and fingerprint and starts_at):
                continue
            if alert.get("status") == "resolved":
                done["resolved"] += self.store.resolve(fingerprint, starts_at, alert.get("endsAt") or starts_at)
                firing = self.store.firing(fingerprint, starts_at)
                waiting = firing and self.store.investigation(firing["investigation_id"])
                if (waiting and waiting["status"] == states.AWAITING_DECISION and waiting["id"] not in done["lapsing"]
                        and all(item["resolved_at"] for item in self.store.firings_of(waiting["id"]))):
                    done["lapsing"].append(waiting["id"])
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

    async def handle(self, notification: dict) -> dict:
        """receive(), and then the lapse of every proposal whose alarms have all resolved."""
        done = self.receive(notification)
        for investigation_id in done["lapsing"]:
            try:
                await self.decide(investigation_id, states.LAPSED, by="incident agent",
                                  note="every alarm behind it resolved before anyone decided")
            except DecisionRefused:
                pass            # decided meanwhile
        return done

    async def decide(self, investigation_id: int, decision: str, by: str, note: str = "") -> dict:
        """A decision on a waiting investigation's proposal: approved, rejected (with a reason) or
        lapsed. A rejection or a lapse resumes its workflow at once, which records the decision in
        the trace and the report. An approval is recorded here and carried out in the background
        (carry_out_one): the answer does not wait for it."""
        if decision not in (states.APPROVED, states.REJECTED, states.LAPSED):
            raise DecisionRefused(f"a decision is approved or rejected, not {decision!r}")
        if decision == states.REJECTED and not note.strip():
            raise DecisionRefused("a rejection needs a reason")
        async with self._deciding:    # one decision at a time: a second one finds the first done
            found = self.store.investigation(investigation_id)
            if found is None:
                raise UnknownInvestigation(investigation_id)
            if found["status"] != states.AWAITING_DECISION:
                raise DecisionRefused(f"investigation {investigation_id} is {found['status']}, not waiting for a decision")
            at = states.utc_now()
            recorded = {"decision": decision, "decided_by": by, "decided_at": at, "decision_note": note.strip() or None}
            if decision == states.APPROVED:
                self.store.update(investigation_id, status=states.CARRYING_OUT, **recorded)
                if self._wake_carrying_out is not None:
                    self._wake_carrying_out.set()
                return self.store.investigation(investigation_id)
            await self._resume(found["trace_id"], {"decision": decision, "by": by, "at": at, "note": note.strip()})
            self.store.update(investigation_id, status=states.COMPLETED, **recorded)
            return self.store.investigation(investigation_id)

    async def carry_out_one(self) -> int | None:
        """Carries out the oldest approved investigation (resumes its workflow with the approval).
        None when nothing is approved and waiting. When the MCP server cannot be reached it stays
        carrying_out and is tried again later; any other failure ends it as failed."""
        approved = self.store.oldest(states.CARRYING_OUT)
        if approved is None:
            return None
        investigation_id = approved["id"]
        decision = {"decision": approved["decision"], "by": approved["decided_by"], "at": approved["decided_at"],
                    "note": approved["decision_note"] or ""}
        try:
            await self._resume(approved["trace_id"], decision)
        except Exception as e:  # every failure is this investigation's outcome
            if refused_connection(e):
                self.store.update(investigation_id, error="waiting: the MCP server refused the connection (is it running?)")
                raise _Unreachable from e
            log.exception("carrying out investigation %s failed", investigation_id)
            self.store.update(investigation_id, status=states.FAILED, finished_at=states.utc_now(),
                              error=f"{type(e).__name__}: {e}")
        else:
            self.store.update(investigation_id, status=states.COMPLETED, finished_at=states.utc_now(), error=None)
        return investigation_id

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
            waiting = document.get("status") == states.AWAITING_DECISION
            proposal = document["result"].get("proposal") or {}
            self.store.update(investigation_id, status=states.AWAITING_DECISION if waiting else states.COMPLETED,
                              finished_at=states.utc_now(), trace_id=document["id"],
                              root_cause=document["result"]["diagnosis"]["root_cause"], proposal=proposal.get("step"),
                              to_retry=len(proposal.get("retry") or proposal.get("rerun") or []) or None)
        return investigation_id

    async def run(self) -> None:
        """Works through the queue, and carries out approvals, for as long as the service runs."""
        self._wake = asyncio.Event()
        self._wake_carrying_out = asyncio.Event()
        requeued = self.store.requeue_running()
        if requeued:
            log.info("queued again after a restart: %s", requeued)
        await asyncio.gather(self._loop(self.process_one, self._wake, "investigation"),
                             self._loop(self.carry_out_one, self._wake_carrying_out, "carrying out"))

    async def _loop(self, work: Callable[[], Awaitable[int | None]], wake: asyncio.Event, what: str) -> None:
        while True:
            try:
                if await work() is not None:
                    continue
                timeout = None
            except _Unreachable as e:
                log.warning("%s postponed: %s", what, e.__cause__)
                timeout = self._retry_after
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout)
            except TimeoutError:
                pass


class _Unreachable(Exception):
    """The model server or the MCP server did not answer."""


class DecisionRefused(Exception):
    """A decision that cannot be taken: the investigation is not waiting for one, or the decision is incomplete."""


class UnknownInvestigation(Exception):
    pass


def runbook_exists(directory) -> Callable[[str], bool]:
    def exists(alert_name: str) -> bool:
        try:
            runbooks.load(alert_name, directory)
        except runbooks.NoRunbook:
            return False
        return True
    return exists
