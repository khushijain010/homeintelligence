"""Dashboard extras: policy editor + tester, routine heatmap, care report CSV, ledger, demo speed."""

import csv
import io
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlmodel import select

from guardian import clock, db, ledger
from guardian.api.routes import dump
from guardian.bus import bus
from guardian.engine import policy, policy_store
from guardian.models import CareSchedule, CareVisit, Resident, Signal
from guardian.oauth import owner
from guardian.state import routine

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- policy


@router.get("/policy/rules")
def rules() -> list[dict]:
    current = policy.current_rules()
    out = []
    for action, rule in current.items():
        out.append(
            {
                "action": action,
                "tier": rule.tier,
                "default_tier": policy.DEFAULT_RULES[action].tier,
                "locked": action in policy.LOCKED,
                "lock_reason": policy.HARD_DENY.get(action) or ("Notifications must always reach people." if action == "notify.*" else None),
                "description": rule.description,
                "conditions": {
                    k: v
                    for k, v in {
                        "min_confidence": rule.min_confidence,
                        "min_signal_devices": rule.min_signal_devices,
                        "requires_occupancy": rule.requires_occupancy,
                        "max_setpoint": rule.max_setpoint,
                    }.items()
                    if v is not None
                },
            }
        )
    return out


class TierIn(BaseModel):
    tier: Literal["auto", "ask", "never"]


@router.put("/policy/rules/{action}")
def set_tier(request: Request, action: str, body: TierIn) -> list[dict]:
    if action not in policy.DEFAULT_RULES:
        raise HTTPException(404, "unknown rule")
    if action in policy.LOCKED:
        raise HTTPException(403, "this rule is locked")
    if body.tier == policy.DEFAULT_RULES[action].tier:
        policy_store.clear(action)
    else:
        policy_store.set_tier(action, body.tier, owner.actor(request))
    ledger.append("policy.tier", {"action": action, "tier": body.tier, "by": owner.actor(request), "at": clock.now()})
    return rules()


class WhatIfIn(BaseModel):
    action_type: str
    confidence: float = 0.85
    signal_devices: int = 2
    occupancy: Literal["empty", "occupied", "unknown"] = "empty"
    target: Literal["none", "healthy", "offline", "fault", "stale"] = "healthy"
    owner_approved: bool = False
    water_incident_open: bool = False
    smoke_incident_open: bool = False
    prior_same_action: int = 0
    setpoint_c: float | None = None


@router.post("/policy/evaluate")
def what_if(body: WhatIfIn) -> dict:
    """Runs the real policy.evaluate on a hypothetical request; nothing is executed."""
    target = None
    if body.target != "none":
        target = policy.Target(
            name="target device", online=body.target != "offline", fault=body.target == "fault", fresh=body.target != "stale"
        )
    ctx = policy.PolicyContext(
        action_type=body.action_type,
        target=target,
        confidence=body.confidence,
        signal_devices=body.signal_devices,
        occupancy=body.occupancy,
        water_incident_open=body.water_incident_open,
        smoke_incident_open=body.smoke_incident_open,
        prior_same_action=body.prior_same_action,
        owner_approved=body.owner_approved,
        params={"setpoint_c": body.setpoint_c} if body.setpoint_c is not None else {},
    )
    d = policy.evaluate(ctx, policy.current_rules())
    return {"outcome": d.outcome, "reasons": d.reasons, "gates": d.gates}


# --------------------------------------------------------------------------- routine


@router.get("/routine")
def routine_view() -> dict:
    b = routine.baseline()
    now = clock.now()
    today = routine.local(now).date()
    today_slots = [0] * routine.SLOTS
    for sig in routine.activity(routine.midnight(today), now):
        t = routine.local(sig.observed_at)
        today_slots[(t.hour * 60 + t.minute) // 30] = 1
    with db.session() as s:
        devs = s.exec(select(Signal).where(Signal.type == "routine.deviation").order_by(Signal.observed_at.desc())).all()  # type: ignore[attr-defined]
    return {
        "learning": routine.learning_status(),
        "slots": b.slots if b else {},
        "today": {"weekday": today.weekday(), "slots": today_slots},
        "first_activity_p90": f"{b.first_activity_p90_min // 60:02d}:{b.first_activity_p90_min % 60:02d}" if b and b.first_activity_p90_min is not None else None,
        "longest_outing_min": b.longest_outing_min if b else None,
        "deviations": [
            {"at": clock.as_utc(d.observed_at).isoformat(), "rule": d.value.get("rule"), "reason": d.value.get("reason")} for d in devs[:20]
        ],
    }


# --------------------------------------------------------------------------- care report


def _visits_for_month(month: str) -> list[dict]:
    with db.session() as s:
        rows = s.exec(select(CareVisit).where(CareVisit.date.startswith(month)).order_by(CareVisit.date)).all()  # type: ignore[attr-defined]
        out = []
        for v in rows:
            sch = s.get(CareSchedule, v.schedule_id)
            who = s.get(Resident, sch.caregiver_id) if sch else None
            out.append({"caregiver": who.name if who else "", **dump(v)})
    return out


@router.get("/care/report")
def care_report(month: str | None = None) -> dict:
    month = month or routine.local(clock.now()).strftime("%Y-%m")
    visits = _visits_for_month(month)
    counts: dict[str, int] = {}
    for v in visits:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    minutes = sum(v["duration_min"] or 0 for v in visits)
    return {"month": month, "visits": len(visits), "counts": counts, "total_minutes": minutes}


@router.get("/care/visits.csv")
def care_csv(month: str | None = None) -> PlainTextResponse:
    month = month or routine.local(clock.now()).strftime("%Y-%m")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "caregiver", "status", "camera_status", "resident_answer", "arrived_at", "departed_at", "duration_min"])
    for v in _visits_for_month(month):
        w.writerow([v["date"], v["caregiver"], v["status"], v["camera_status"], v["resident_answer"], v["arrived_at"], v["departed_at"], v["duration_min"]])
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="care-visits-{month}.csv"'}
    )


# --------------------------------------------------------------------------- ledger


@router.get("/ledger")
def get_ledger(limit: int = 100, incident_id: str | None = None) -> list[dict]:
    return [dump(e) for e in ledger.entries(min(limit, 500), incident_id)]


@router.get("/ledger/verify")
def verify_ledger() -> dict:
    return ledger.verify()


# --------------------------------------------------------------------------- demo speed


class SpeedIn(BaseModel):
    speed: Literal[1, 10, 60]


@router.get("/demo/speed")
def get_speed() -> dict:
    return {"speed": clock.speed()}


@router.post("/demo/speed")
def set_speed(body: SpeedIn) -> dict:
    clock.set_speed(body.speed)
    bus.publish("home", {"speed": body.speed})
    return {"speed": clock.speed()}

