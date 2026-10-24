"""The agent's operator login (PayFlow client `incident-agent-operator`): the only code in the agent
that can change anything in PayFlow, and only to carry out what a person approved.

It lives here, in the agent, and not in the MCP server: the MCP server's tools must stay read-only,
and its login (`incident-agent`) cannot retry anything. This login's secret
(config.PAYFLOW_OPERATOR_CLIENT_SECRET) is read by nothing else, and it can only retry parked
payment and payout sagas (scopes payments:operate, payouts:operate) and read them back.

Ids are as the saga tools give them: a payment (pay_...) or a payout (po_...). PayFlow retries a
saga only while it is parked (REQUIRES_ATTENTION) and answers 409 otherwise: a second guard behind
the agent's own check.
"""
import asyncio
import time
from typing import Any

import httpx

PARKED = "REQUIRES_ATTENTION"
RUNS = "/api/v1/reconciliation/runs"
# PayFlow reconciles a day inside the request that asks for it: seconds for an ordinary day, half an
# hour for the load test's. The answer is waited for this long; a run still going is then watched.
RERUN_WAIT_SECONDS = 60
# A token is renewed this long before it expires.
_RENEW_BEFORE_SECONDS = 60


class OperatorUnavailable(Exception):
    """PayFlow did not answer, or refused the operator login itself."""


class Refused(Exception):
    """PayFlow refused the action: the saga is not parked (any more), or there is no such payment."""


class Operator:
    def __init__(self, base_url: str, client_id: str, client_secret: str, http: httpx.AsyncClient | None = None):
        self._base_url = base_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def saga(self, saga_id: str, saga: str | None = None) -> dict:
        """The saga as it is now: {"id", "saga", "state", "stuck_state", "parked_for", "failure_code",
        "last_error", "finished"}. For a payment (which can have several), the one whose sagaId is
        `saga` when given; else the one waiting for an operator if there is one, else its most recent."""
        return _summary(saga_id, await self._call("GET", f"{_path(saga_id)}/saga"), saga)

    async def retry(self, saga_id: str) -> dict:
        """Resumes a parked saga at the step it stopped at. Returns it as it is just after."""
        return _summary(saga_id, await self._call("POST", f"{_path(saga_id)}/saga/retry"))

    async def runs(self, day: str) -> list[dict]:
        """A day's reconciliation runs, newest first: {"id", "status", "started_at", "finished_at",
        "error", "discrepancies"}."""
        answer = await self._call("GET", RUNS, params={"date": day})
        return sorted((_run(item) for item in answer), key=lambda run: run["started_at"] or "", reverse=True)

    async def rerun(self, day: str) -> dict | None:
        """Reconciles a day again. The run as PayFlow finished it; None when it was still going after
        RERUN_WAIT_SECONDS (it goes on in PayFlow; read it with runs()). Refused (409) when a run of
        that day is already going."""
        try:
            answer = await self._call("POST", RUNS, json={"date": day}, timeout=RERUN_WAIT_SECONDS)
        except _StillGoing:
            return None
        return _run(answer.get("run", answer))

    async def _call(self, method: str, path: str, params: dict | None = None, json: dict | None = None,
                    timeout: float | None = None) -> Any:
        async with self._session() as http:
            for attempt in (1, 2):
                try:
                    response = await http.request(method, path, params=params, json=json,
                                                  headers={"Authorization": f"Bearer {await self._current_token(http)}"},
                                                  **({"timeout": timeout} if timeout is not None else {}))
                except httpx.ReadTimeout as e:
                    if timeout is not None:     # sent and taken, not answered yet: the caller watches it
                        raise _StillGoing from e
                    raise OperatorUnavailable(f"PayFlow did not answer {method} {path} (ReadTimeout)") from e
                except httpx.HTTPError as e:
                    raise OperatorUnavailable(f"PayFlow did not answer {method} {path} ({type(e).__name__})") from e
                if response.status_code == 401 and attempt == 1:
                    self._token = None        # expired or revoked: one fresh token, then give up
                    continue
                if response.status_code in (404, 409):
                    raise Refused(f"PayFlow refused {method} {path}: {_detail(response)}")
                if response.status_code >= 400:
                    raise OperatorUnavailable(f"PayFlow answered {response.status_code} to {method} {path}: {_detail(response)}")
                return response.json()
        raise OperatorUnavailable(f"PayFlow refused the operator login for {method} {path}")

    async def _current_token(self, http: httpx.AsyncClient) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - _RENEW_BEFORE_SECONDS:
                return self._token
            if not self._client_secret:
                raise OperatorUnavailable("No operator secret: run scripts/write_env.py (PAYFLOW_OPERATOR_CLIENT_SECRET)")
            try:
                answer = await http.post("/oauth2/token", data={"grant_type": "client_credentials"},
                                         auth=(self._client_id, self._client_secret))
            except httpx.HTTPError as e:
                raise OperatorUnavailable(f"PayFlow's token endpoint did not answer ({type(e).__name__})") from e
            if answer.status_code != 200:
                raise OperatorUnavailable(f"PayFlow refused the client id or secret of {self._client_id}")
            body = answer.json()
            self._token, self._expires_at = body["access_token"], time.monotonic() + body.get("expires_in", 300)
            return self._token

    def _session(self):
        if self._http is not None:
            return _Borrowed(self._http)
        return httpx.AsyncClient(base_url=self._base_url, timeout=15)


class _StillGoing(Exception):
    """A request PayFlow took and is still working on (only for a call given its own timeout)."""


def _run(item: dict) -> dict:
    return {"id": str(item.get("runId") or item.get("id")), "status": item.get("status"), "started_at": item.get("startedAt"),
            "finished_at": item.get("finishedAt"), "error": item.get("error"), "discrepancies": item.get("discrepancyCount")}


class _Borrowed:
    """A client handed in (tests): used, not closed."""

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def __aenter__(self):
        return self._http

    async def __aexit__(self, *exc):
        return False


def _path(saga_id: str) -> str:
    if saga_id.startswith("pay_"):
        return f"/api/v1/payments/{saga_id}"
    if saga_id.startswith("po_"):
        return f"/api/v1/payouts/{saga_id}"
    raise ValueError(f"not a payment (pay_...) or payout (po_...) id: {saga_id!r}")


def _summary(saga_id: str, view: Any, wanted: str | None = None) -> dict:
    """A payment's view is a list of its sagas (the payment's and its refunds'); a payout's, one saga."""
    sagas = view if isinstance(view, list) else [view]
    if not sagas:
        raise Refused(f"PayFlow has no saga for {saga_id}")
    if wanted is not None:
        saga = next((item for item in sagas if str(item.get("sagaId")) == wanted), None)
        if saga is None:
            raise Refused(f"PayFlow has no saga {wanted} for {saga_id}")
    else:
        parked = [item for item in sagas if item.get("state") == PARKED]
        saga = parked[0] if parked else max(sagas, key=lambda item: item.get("createdAt") or "")
    return {"id": saga_id, "saga": None if saga.get("sagaId") is None else str(saga["sagaId"]),
            "state": saga.get("state"), "stuck_state": saga.get("stuckState"), "parked_for": _parked_for(saga),
            "failure_code": saga.get("failureCode"), "last_error": saga.get("lastError"),
            "finished": saga.get("finishedAt") is not None}


def _parked_for(saga: dict) -> str | None:
    """For a parked saga, the code it parked for: PayFlow records it as its last step's outcome."""
    steps = saga.get("steps") or []
    if saga.get("state") != PARKED or not steps:
        return None
    _, last = max(enumerate(steps), key=lambda pair: (pair[1].get("occurredAt") or "", pair[0]))
    return last.get("outcome")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("message") or body.get("detail") or body)[:200] if isinstance(body, dict) else str(body)[:200]
