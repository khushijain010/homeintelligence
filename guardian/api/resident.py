"""Voice Console (simulated Alexa+ check-in) and resident-facing endpoints."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from guardian import clock
from guardian.api.routes import dump
from guardian.bus import bus
from guardian.config import settings
from guardian.engine import escalation, incidents, report
from guardian.oauth import owner
from guardian.state import checkins, routine

router = APIRouter()
STATIC = Path(__file__).resolve().parents[1] / "static"


@router.get("/resident", include_in_schema=False)
def resident_page() -> FileResponse:
    return FileResponse(STATIC / "resident.html")


@router.get("/api/checkins/pending")
def pending_checkins() -> list[dict]:
    rows = checkins.pending()
    return [dump(c) for c in rows[-1:]]  # one question at a time: the newest


class AnswerIn(BaseModel):
    answer: str | None = None
    transcript: str | None = None


@router.post("/api/checkins/{checkin_id}/answer")
def answer_checkin(checkin_id: str, body: AnswerIn) -> dict:
    try:
        result = checkins.answer(checkin_id, body.answer, body.transcript)
    except KeyError:
        raise HTTPException(404, "check-in not found") from None
    return {"result": result}


def resident_status() -> dict:
    p = routine.active_pause()
    return {
        "paused_until": clock.as_utc(p.until).isoformat() if p else None,
        "paused_by": p.set_by if p else None,
        "learning": routine.learning_status(),
        "last_activity": routine.describe_last_activity(),
        "emergency_number_fire": settings.emergency_number_fire,
    }


@router.get("/api/resident/status")
def get_resident_status() -> dict:
    return resident_status()


class PauseIn(BaseModel):
    hours: Literal[1, 4, 24]


@router.post("/api/resident/pause")
def pause(body: PauseIn) -> dict:
    routine.pause(body.hours, by="resident")
    bus.publish("home", {"paused": True})
    return resident_status()


@router.post("/api/resident/resume")
def resume() -> dict:
    routine.resume()
    bus.publish("home", {"paused": False})
    return resident_status()


@router.post("/api/incidents/{incident_id}/ack")
def acknowledge(request: Request, incident_id: str) -> dict:
    inc = incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    if not escalation.acknowledge(incident_id, owner.actor(request)):
        raise HTTPException(409, "already acknowledged")
    incidents.update(incident_id, report_text=report.build(incident_id))
    return {"ok": True}
