from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import SQLModel, select

from guardian import clock, db
from guardian.ai import report_llm
from guardian.bus import bus
from guardian.config import settings
from guardian.demo import director
from guardian.devices import registry, virtual
from guardian.engine import approvals, incidents
from guardian.engine.runner import DECISIONS
from guardian.models import Action, CareSchedule, CareVisit, Incident, Resident
from guardian.oauth import owner
from guardian.state import home_state

router = APIRouter(prefix="/api")


def dump(obj: SQLModel) -> dict[str, Any]:
    out = obj.model_dump()
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = clock.as_utc(v).isoformat()
    return out


# --------------------------------------------------------------------------- home


@router.get("/home")
def get_home() -> dict:
    from guardian.api.resident import resident_status

    return {
        **dump(home_state.get_home()),
        **home_state.snapshot(),
        **resident_status(),
        "now": clock.now().isoformat(),
        "speed": clock.speed(),
        "approval_timeout_seconds": settings.approval_timeout_seconds,
        "smoke_open": bool(incidents.open_incidents("smoke_co")),
    }


class ModeIn(BaseModel):
    mode: Literal["home", "away", "night"]


@router.patch("/home")
def patch_home(body: ModeIn) -> dict:
    home_state.set_mode(body.mode)
    return get_home()


# --------------------------------------------------------------------------- devices


def _device_json(d) -> dict:
    usable, problems = registry.reliability(d)
    return {**dump(d), "usable": usable, "problems": problems, "simulated": d.source == "virtual"}


@router.get("/devices")
def get_devices() -> list[dict]:
    order = {"doorbell": 0, "contact_sensor": 1, "flood_sensor": 2, "valve": 3, "camera": 4}
    devices = [d for d in registry.all_devices() if d.kind != "routine"]
    return [_device_json(d) for d in sorted(devices, key=lambda d: (order.get(d.kind, 9), d.name))]


class SimulateIn(BaseModel):
    action: Literal["fault_on_close", "fault", "reset", "freeze", "heatwave", "normal", "smoke", "co", "clear"]


@router.post("/devices/{device_id}/simulate")
def simulate(device_id: str, body: SimulateIn) -> dict:
    """Demo controls for the virtual valve. Failures only — closing goes through the policy engine."""
    from guardian.devices import monitor

    dev = virtual.get_virtual(device_id)
    if isinstance(dev, virtual.VirtualThermostat) and body.action in ("freeze", "heatwave", "normal"):
        dev.simulate(*{"freeze": (3.0, 1.0), "heatwave": (37.0, 39.0), "normal": (24.0, 24.0)}[body.action])
        monitor.check_temperatures()
        return _device_json(registry.get(device_id))
    if isinstance(dev, virtual.VirtualSmokeAlarm) and body.action in ("smoke", "co", "clear"):
        if body.action == "clear":
            dev.clear()
        else:
            dev.trigger(body.action)
        return _device_json(registry.get(device_id))
    valve = virtual.get_valve(device_id)
    if valve is None or body.action not in ("fault_on_close", "fault", "reset"):
        raise HTTPException(400, "unsupported simulation for this device")
    if body.action == "reset":
        if incidents.open_incidents("water_leak"):
            raise HTTPException(409, "cannot reopen the valve while a water incident is open")
        valve.reset()
    elif body.action == "fault":
        valve.set_fault()
    else:
        valve.fault_on_close = True
    return _device_json(registry.get(device_id))


# --------------------------------------------------------------------------- incidents


def _action_json(a: Action) -> dict:
    out = dump(a)
    if a.approval_status == "pending":
        expires = clock.as_utc(a.created_at) + timedelta(seconds=settings.approval_timeout_seconds)
        out["expires_at"] = expires.isoformat()
    return out


@router.get("/incidents")
def list_incidents() -> list[dict]:
    with db.session() as s:
        rows = s.exec(select(Incident).order_by(Incident.opened_at.desc())).all()  # type: ignore[attr-defined]
    return [dump(i) for i in rows]


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    inc = incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    llm = report_llm.latest(incident_id)
    return {
        **dump(inc),
        "evidence": [dump(e) for e in incidents.evidence(incident_id)],
        "actions": [_action_json(a) for a in incidents.actions(incident_id)],
        # bedrock = validated against evidence; otherwise the template in report_text is shown
        "llm_report": dump(llm) if llm and llm.source == "bedrock" else None,
    }


class DecisionIn(BaseModel):
    choice: Literal["known_person", "handle_it", "escalate"]


@router.post("/incidents/{incident_id}/decision")
def decide(incident_id: str, body: DecisionIn) -> dict:
    inc = incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    if inc.playbook_id != "suspicious_entry" or inc.status != "awaiting_approval":
        raise HTTPException(409, "this incident is not waiting for a decision")
    if any(e.kind == "human_decision" and "choice" in e.payload for e in incidents.evidence(incident_id)):
        raise HTTPException(409, "decision already recorded")
    incidents.add_evidence(
        incident_id, "human_decision", f"Owner chose: {DECISIONS[body.choice][1]}", {"choice": body.choice}
    )
    return get_incident(incident_id)


def _set_approval(request: Request, action_id: str, approve: bool) -> dict:
    try:
        action = approvals.decide(action_id, approve, by=owner.actor(request))
    except approvals.ApprovalError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return _action_json(action)


@router.post("/actions/{action_id}/approve")
def approve(request: Request, action_id: str) -> dict:
    return _set_approval(request, action_id, True)


@router.post("/actions/{action_id}/reject")
def reject(request: Request, action_id: str) -> dict:
    return _set_approval(request, action_id, False)


# --------------------------------------------------------------------------- care


@router.get("/care/visits")
def care_visits() -> list[dict]:
    since = (clock.now() - timedelta(days=7)).date().isoformat()
    with db.session() as s:
        visits = s.exec(select(CareVisit).where(CareVisit.date >= since).order_by(CareVisit.date.desc())).all()  # type: ignore[attr-defined]
        out = []
        for v in visits:
            sch = s.get(CareSchedule, v.schedule_id)
            who = s.get(Resident, sch.caregiver_id) if sch else None
            out.append(
                {
                    **dump(v),
                    "caregiver": who.name if who else "?",
                    "window": f"{sch.start_local}–{sch.end_local}" if sch else "",
                }
            )
    return out


# --------------------------------------------------------------------------- demo


@router.post("/demo/run/{name}")
async def demo_run(name: str) -> dict:
    if name not in director.SCENARIOS:
        raise HTTPException(404, f"unknown scenario; have {sorted(director.SCENARIOS)}")
    await director.run(name)
    bus.publish("reset", {})
    return {"ok": True, "scenario": name}


@router.post("/demo/reset")
async def demo_reset() -> dict:
    await director.reset()
    bus.publish("reset", {})
    return {"ok": True}


@router.get("/demo/scenarios")
def demo_scenarios() -> list[str]:
    return list(director.SCENARIOS)
