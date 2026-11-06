"""HTTP API: Alertmanager webhook in, incidents and approvals out.

POST /webhooks/alertmanager     Alertmanager payload (each firing alert becomes an incident)
POST /incidents                 any JSON alert {labels, annotations}
GET  /incidents                 recent incidents
GET  /incidents/{id}            incident with analysis and timeline
GET  /incidents/{id}/postmortem markdown draft
GET  /approvals                 actions waiting for a human
POST /incidents/{id}/approve    {"by": "name", "comment": ""}
POST /incidents/{id}/deny       {"by": "name", "comment": ""}
GET  /healthz
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from agent_harness_sdk import PendingApprovals, get_logger

from .commander import IncidentCommander

log = get_logger("incident_commander.api")


class Review(BaseModel):
    by: str
    comment: str = ""


def create_app(commander: IncidentCommander | None = None, approvals: PendingApprovals | None = None) -> FastAPI:
    approvals = approvals or PendingApprovals(timeout_s=900)
    commander = commander or IncidentCommander(approver=approvals)
    app = FastAPI(title="Incident Commander", version="0.1.0")
    tasks: set[asyncio.Task] = set()

    def start(alert: dict) -> None:
        task = asyncio.create_task(commander.handle(alert))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "running": len(tasks)}

    @app.post("/webhooks/alertmanager", status_code=202)
    async def alertmanager(payload: dict):
        firing = [a for a in payload.get("alerts", []) if a.get("status", "firing") == "firing"]
        for alert in firing:
            start(alert)
        log.info("accepted %d firing alert(s)", len(firing))
        return {"accepted": len(firing)}

    @app.post("/incidents", status_code=202)
    async def new_incident(alert: dict):
        start(alert)
        return {"accepted": 1}

    @app.get("/incidents")
    async def incidents():
        return commander.store.list()

    @app.get("/incidents/{iid}")
    async def incident(iid: str):
        inc = commander.store.get(iid)
        if inc is None:
            raise HTTPException(404, "no such incident")
        inc.pop("postmortem", None)
        return inc

    @app.get("/incidents/{iid}/postmortem", response_class=PlainTextResponse)
    async def postmortem(iid: str):
        inc = commander.store.get(iid)
        if inc is None or not inc.get("postmortem"):
            raise HTTPException(404, "no postmortem yet")
        return inc["postmortem"]

    @app.get("/approvals")
    async def pending():
        return [
            {
                "incident": rid,
                "action": req.action,
                "arguments": req.arguments,
                "rationale": req.rationale,
                "risk": req.risk,
                "confidence": req.confidence,
            }
            for rid, (req, _) in approvals.pending.items()
        ]

    @app.post("/incidents/{iid}/approve")
    async def approve(iid: str, review: Review):
        if not approvals.resolve(iid, True, review.by, review.comment):
            raise HTTPException(404, "nothing waiting for approval on this incident")
        return {"approved": True}

    @app.post("/incidents/{iid}/deny")
    async def deny(iid: str, review: Review):
        if not approvals.resolve(iid, False, review.by, review.comment):
            raise HTTPException(404, "nothing waiting for approval on this incident")
        return {"approved": False}

    return app
