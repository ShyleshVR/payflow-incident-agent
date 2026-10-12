"""PayFlow's own API, through its gateway, as the read-only service client "incident-agent".

A token is fetched with the client's id and secret (OAuth2 client credentials), kept until
shortly before it expires, and sent as a Bearer header. If PayFlow answers 401 anyway (its
signing key changed, the token was cut short), one fresh token is fetched and the call repeated.
"""
import asyncio
import time
from typing import Any

import httpx

from mcp_server import config

# A token is replaced this long before it expires, so a call never starts with a dying token.
_RENEW_BEFORE_SECONDS = 60


class PayflowUnavailable(Exception):
    """PayFlow can't be asked as this client: no secret, credentials refused, or a missing scope."""


class NotFound(Exception):
    """PayFlow answered: there is no such payment, payout or run."""


class BadRequest(Exception):
    """PayFlow answered: the request is wrong (e.g. an id that is not one of its ids)."""


_token: str | None = None
_token_expires_at = 0.0
_token_lock = asyncio.Lock()


def forget_token() -> None:
    global _token, _token_expires_at, _token_lock
    _token, _token_expires_at = None, 0.0
    _token_lock = asyncio.Lock()  # a lock belongs to the event loop that first waited on it


async def get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with _client() as client:
        return await _call(client, "GET", path, params=params)


async def post(path: str, body: dict[str, Any]) -> Any:
    """Only PayFlow's read-only lookups are POSTs for this client; its scopes allow nothing else."""
    async with _client() as client:
        return await _call(client, "POST", path, json=body)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.PAYFLOW_URL, timeout=10)


async def _call(client: httpx.AsyncClient, method: str, path: str, **request: Any) -> Any:
    response = await _send(client, method, path, request)
    if response.status_code == 401:
        forget_token()
        response = await _send(client, method, path, request)
    if response.status_code == 404:
        raise NotFound(path)
    if response.status_code == 400:
        raise BadRequest(path)
    if response.status_code in (401, 403):
        raise PayflowUnavailable(f"PayFlow refused {method} {path} for client {config.PAYFLOW_CLIENT_ID} "
                                 f"(HTTP {response.status_code}): the client lacks the scope for it")
    response.raise_for_status()
    return response.json()


async def _send(client: httpx.AsyncClient, method: str, path: str, request: dict[str, Any]) -> httpx.Response:
    token = await _current_token(client)
    return await client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **request)


async def _current_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires_at
    async with _token_lock:  # concurrent calls share one token request
        if _token is None or time.monotonic() >= _token_expires_at:
            if not config.PAYFLOW_CLIENT_SECRET:
                raise PayflowUnavailable("PAYFLOW_CLIENT_SECRET is not set for the MCP server "
                                         "(locally: run scripts/write_env.py once)")
            response = await client.post("/oauth2/token", data={"grant_type": "client_credentials"},
                                         auth=(config.PAYFLOW_CLIENT_ID, config.PAYFLOW_CLIENT_SECRET))
            if response.status_code in (400, 401):
                raise PayflowUnavailable(f"PayFlow refused the client id or secret of {config.PAYFLOW_CLIENT_ID}")
            response.raise_for_status()
            granted = response.json()
            _token = granted["access_token"]
            _token_expires_at = time.monotonic() + max(0, granted.get("expires_in", 0) - _RENEW_BEFORE_SECONDS)
        return _token
