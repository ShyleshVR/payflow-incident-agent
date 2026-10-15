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

    async def saga(self, saga_id: str) -> dict:
        """The saga as it is now: {"id", "state", "stuck_state", "failure_code", "last_error", "finished"}.
        For a payment, the saga waiting for an operator if there is one, else its most recent."""
        return _summary(saga_id, await self._call("GET", f"{_path(saga_id)}/saga"))

    async def retry(self, saga_id: str) -> dict:
        """Resumes a parked saga at the step it stopped at. Returns it as it is just after."""
        return _summary(saga_id, await self._call("POST", f"{_path(saga_id)}/saga/retry"))

    async def _call(self, method: str, path: str) -> Any:
        async with self._session() as http:
            for attempt in (1, 2):
                try:
                    response = await http.request(method, path, headers={"Authorization": f"Bearer {await self._current_token(http)}"})
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


def _summary(saga_id: str, view: Any) -> dict:
    """A payment's view is a list of its sagas (the payment's and its refunds'); a payout's, one saga."""
    if isinstance(view, list):
        if not view:
            raise Refused(f"PayFlow has no saga for {saga_id}")
        parked = [saga for saga in view if saga.get("state") == PARKED]
        saga = parked[0] if parked else max(view, key=lambda item: item.get("createdAt") or "")
    else:
        saga = view
    return {"id": saga_id, "state": saga.get("state"), "stuck_state": saga.get("stuckState"),
            "failure_code": saga.get("failureCode"), "last_error": saga.get("lastError"),
            "finished": saga.get("finishedAt") is not None}


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("message") or body.get("detail") or body)[:200] if isinstance(body, dict) else str(body)[:200]
