"""The agent's HTTP interface (FastAPI). Run it with:  payflow-agent serve

    POST /alerts                        Alertmanager's webhook: each new firing starts one investigation
    GET  /investigations[?status=...]   the most recent investigations, newest first
    GET  /investigations/{id}           one investigation: its state, firings, and report when there is one
    POST /investigations/{id}/decision  approve or reject its proposal: {"decision": "approve" | "reject",
                                        "by": "...", "note": "..."} (a rejection needs a note). An
                                        approval is answered at once and carried out in the background

A token (config.AGENT_TOKEN), when set, is required on every request as "Authorization: Bearer
<token>": Alertmanager sends it from its receiver's settings.
"""
import asyncio
import contextlib
import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from agent import config, report
from agent import run as runs
from agent import trace as traces
from api import store as states
from api.service import DecisionRefused, Service, UnknownInvestigation, runbook_exists
from api.store import Store


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    by: str = ""
    note: str = ""


def create_app(service: Service, runs_dir: Path, token: str | None = None, resources=()) -> FastAPI:
    """resources: async context managers entered for the app's lifetime, before the worker starts
    (the checkpointer's connection)."""
    def authorised(authorization: str | None = Header(default=None)) -> None:
        if token and not secrets.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="a valid bearer token is required")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            for resource in resources:
                await stack.enter_async_context(resource)
            worker = asyncio.create_task(service.run())
            try:
                yield
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    app = FastAPI(title="payflow incident agent", lifespan=lifespan, dependencies=[Depends(authorised)])

    # async: they run on the event loop, beside the worker, never in another thread
    @app.post("/alerts")
    async def alerts(notification: dict) -> dict:
        return await service.handle(notification)

    @app.get("/investigations")
    async def investigations(limit: int = 50, status: str | None = None) -> list[dict]:
        return service.store.investigations(min(max(limit, 1), 500), status)

    @app.post("/investigations/{investigation_id}/decision")
    async def decision(investigation_id: int, body: Decision) -> dict:
        try:
            return await service.decide(investigation_id, states.APPROVED if body.decision == "approve" else states.REJECTED,
                                        by=body.by.strip() or "unknown", note=body.note)
        except UnknownInvestigation as e:
            raise HTTPException(status_code=404, detail=f"no investigation {investigation_id}") from e
        except DecisionRefused as e:
            raise HTTPException(status_code=409 if "not waiting" in str(e) else 400, detail=str(e)) from e

    @app.get("/investigations/{investigation_id}")
    async def investigation(investigation_id: int) -> dict:
        found = service.store.investigation(investigation_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no investigation {investigation_id}")
        detail = found | {"firings": service.store.firings_of(investigation_id), "report": None}
        if found["trace_id"]:
            path = runs_dir / f"{found['trace_id']}.json"
            if path.is_file():
                detail["report"] = report.render(traces.load(path))
        return detail

    return app


class Checkpoints:
    """The paused workflows' saved state (config.AGENT_CHECKPOINTS), open while the app runs."""

    def __init__(self, path: Path):
        self._path = path
        self._opened = None
        self.saver = None

    async def __aenter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._opened = AsyncSqliteSaver.from_conn_string(str(self._path))
        self.saver = await self._opened.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._opened.__aexit__(*exc)


def default_app() -> FastAPI:
    """The app as `payflow-agent serve` runs it: the live system, the local model, the settings."""
    checkpoints = Checkpoints(config.AGENT_CHECKPOINTS)

    async def investigate(alert_name: str):
        return await runs.investigate_live(alert_name, config.MCP_URL, config.RUNS_DIR, checkpointer=checkpoints.saver,
                                           ask_for_decision=True)

    operator = runs.operator_login()

    async def resume(trace_id: str, decision: dict):
        return await runs.resume_live(config.RUNS_DIR / f"{trace_id}.json", decision, config.MCP_URL, checkpoints.saver,
                                      operator)

    service = Service(Store(config.AGENT_DB), investigate, runbook_exists(config.RUNBOOKS_DIR), resume=resume)
    return create_app(service, config.RUNS_DIR, config.AGENT_TOKEN, resources=[checkpoints])
