"""The agent's HTTP interface (FastAPI). Run it with:  payflow-agent serve

    POST /alerts                  Alertmanager's webhook: each new firing starts one investigation
    GET  /investigations          the most recent investigations, newest first
    GET  /investigations/{id}     one investigation: its state, firings, and report when there is one

A token (config.AGENT_TOKEN), when set, is required on every request as "Authorization: Bearer
<token>": Alertmanager sends it from its receiver's settings.
"""
import asyncio
import contextlib
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException

from agent import config, report
from agent import run as runs
from agent import trace as traces
from api.service import Service, runbook_exists
from api.store import Store


def create_app(service: Service, runs_dir: Path, token: str | None = None) -> FastAPI:
    def authorised(authorization: str | None = Header(default=None)) -> None:
        if token and not secrets.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="a valid bearer token is required")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = asyncio.create_task(service.run())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(title="payflow incident agent", lifespan=lifespan, dependencies=[Depends(authorised)])

    @app.post("/alerts")
    def alerts(notification: dict) -> dict:
        return service.receive(notification)

    @app.get("/investigations")
    def investigations(limit: int = 50) -> list[dict]:
        return service.store.investigations(min(max(limit, 1), 500))

    @app.get("/investigations/{investigation_id}")
    def investigation(investigation_id: int) -> dict:
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


def default_app() -> FastAPI:
    """The app as `payflow-agent serve` runs it: the live system, the local model, the settings."""
    async def investigate(alert_name: str):
        return await runs.investigate_live(alert_name, config.MCP_URL, config.RUNS_DIR)

    service = Service(Store(config.AGENT_DB), investigate, runbook_exists(config.RUNBOOKS_DIR))
    return create_app(service, config.RUNS_DIR, config.AGENT_TOKEN)
