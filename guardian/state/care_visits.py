"""Caregiver visit verification from front-door activity. Identity is never inferred:
a visit is 'someone came to the door in the window', nothing more."""

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import select

from guardian import clock, db
from guardian.bus import bus
from guardian.config import settings
from guardian.engine.playbooks import EXTERIOR_ROOMS
from guardian.models import CareSchedule, CareVisit, Device, Resident, Signal

log = logging.getLogger("guardian.care")

ARRIVAL_BEFORE = timedelta(minutes=30)
ARRIVAL_AFTER = timedelta(minutes=60)
LATE_AFTER = timedelta(minutes=15)
QUIET = timedelta(minutes=20)
DEPARTURE_SEARCH_AFTER_END = timedelta(minutes=60)

# Windows that started before monitoring began are not judged (no false no-shows at boot).
monitoring_since: datetime = clock.now()


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.home_timezone)


def window(schedule: CareSchedule, day: date) -> tuple[datetime, datetime]:
    def at(hhmm: str) -> datetime:
        h, m = map(int, hhmm.split(":"))
        return datetime.combine(day, time(h, m), tzinfo=_tz()).astimezone(UTC)

    return at(schedule.start_local), at(schedule.end_local)


def _front_door_devices() -> list[Device]:
    with db.session() as s:
        devs = s.exec(select(Device)).all()
    return [d for d in devs if d.room in EXTERIOR_ROOMS and d.kind in ("doorbell", "camera", "contact_sensor")]


def _signals(types: tuple[str, ...], device_ids: list[str], start: datetime, end: datetime) -> list[Signal]:
    with db.session() as s:
        rows = s.exec(
            select(Signal)
            .where(Signal.type.in_(types))  # type: ignore[attr-defined]
            .where(Signal.device_id.in_(device_ids))  # type: ignore[attr-defined]
            .where(Signal.observed_at >= start)
            .where(Signal.observed_at <= end)
            .order_by(Signal.observed_at)  # type: ignore[arg-type]
        ).all()
    return list(rows)


def assess(schedule: CareSchedule, day: date, now: datetime) -> dict | None:
    """Return the visit verdict for `day`, or None if it can't be decided yet."""
    start, end = window(schedule, day)
    if now < start - ARRIVAL_BEFORE:
        return None
    devices = _front_door_devices()
    ids = [d.id for d in devices]
    cameras = [d for d in devices if d.kind in ("doorbell", "camera")]

    arrivals = _signals(("doorbell.pressed", "motion.human"), ids, start - ARRIVAL_BEFORE, start + ARRIVAL_AFTER)
    camera_down = bool(
        _signals(("device.offline",), [c.id for c in cameras], start - ARRIVAL_BEFORE, end)
    ) or any(not c.online for c in cameras)

    if not arrivals:
        if now < start + ARRIVAL_AFTER:
            return None
        status = "unverified" if camera_down else "no_show"
        return {"status": status, "arrived_at": None, "departed_at": None, "duration_min": None}

    arrived = clock.as_utc(arrivals[0].observed_at)
    motions = _signals(("motion.human",), ids, arrived, end + DEPARTURE_SEARCH_AFTER_END)
    last = clock.as_utc(motions[-1].observed_at) if motions else arrived
    if now < max(end, last) + QUIET:
        return None  # visit may still be going on
    departed = last
    duration = int((departed - arrived).total_seconds() // 60)
    if camera_down:
        status = "unverified"
    elif arrived - start > LATE_AFTER:
        status = "late"
    elif duration < schedule.min_duration_min:
        status = "short"
    else:
        status = "completed"
    return {"status": status, "arrived_at": arrived, "departed_at": departed, "duration_min": duration}


def visit_in_progress(now: datetime | None = None) -> bool:
    """Someone arrived for a scheduled visit and the visit hasn't been closed out."""
    now = now or clock.now()
    day = now.astimezone(_tz()).date()
    with db.session() as s:
        schedules = s.exec(select(CareSchedule)).all()
    ids = [d.id for d in _front_door_devices()]
    for sch in schedules:
        if day.weekday() not in sch.weekdays:
            continue
        start, end = window(sch, day)
        if not (start - ARRIVAL_BEFORE <= now <= end + DEPARTURE_SEARCH_AFTER_END):
            continue
        if _signals(("doorbell.pressed", "motion.human"), ids, start - ARRIVAL_BEFORE, start + ARRIVAL_AFTER):
            if assess(sch, day, now) is None:
                return True
    return False


def check(now: datetime | None = None) -> list[CareVisit]:
    """Decide today's visits that are ready; open a no-show incident when needed."""
    from guardian.engine import incidents, runner  # local import: engine imports care_visits

    now = now or clock.now()
    day = now.astimezone(_tz()).date()
    created: list[CareVisit] = []
    with db.session() as s:
        schedules = s.exec(select(CareSchedule)).all()
    for sch in schedules:
        if day.weekday() not in sch.weekdays:
            continue
        start, _ = window(sch, day)
        if start - ARRIVAL_BEFORE < monitoring_since:
            continue
        with db.session() as s:
            done = s.exec(
                select(CareVisit).where(CareVisit.schedule_id == sch.id).where(CareVisit.date == day.isoformat())
            ).first()
        if done:
            continue
        verdict = assess(sch, day, now)
        if verdict is None:
            continue
        visit = CareVisit(schedule_id=sch.id, date=day.isoformat(), camera_status=verdict["status"], **verdict)
        with db.session() as s:
            s.add(visit)
            caregiver = s.get(Resident, sch.caregiver_id)
            s.commit()
        created.append(visit)
        bus.publish("care", {"id": visit.id})
        name = caregiver.name if caregiver else "the caregiver"
        from guardian.state import checkins

        checkins.create("care_confirm", f"Did {name} come today?", f"क्या {name} आज आई थीं?", visit_id=visit.id)
        if visit.status == "no_show":
            inc = incidents.open_incident("caregiver_no_show", f"Caregiver no-show — {name}")
            local = start.astimezone(_tz()).strftime("%H:%M")
            incidents.add_evidence(
                inc.id,
                "verification",
                f"No front-door arrival for {name} between {sch.start_local} −30 min and +60 min "
                f"(expected {local})",
                {"visit_id": visit.id, "schedule_id": sch.id, "status": visit.status},
            )
            runner.start(inc.id)
    return created


def backfill(days: list[date], now: datetime | None = None) -> int:
    """Record verdicts for past days (seeded history) without questions or incidents."""
    now = now or clock.now()
    count = 0
    with db.session() as s:
        schedules = s.exec(select(CareSchedule)).all()
    for day in days:
        for sch in schedules:
            if day.weekday() not in sch.weekdays:
                continue
            verdict = assess(sch, day, now)
            if verdict is None:
                continue
            with db.session() as s:
                s.add(CareVisit(schedule_id=sch.id, date=day.isoformat(), camera_status=verdict["status"], **verdict))
                s.commit()
            count += 1
    return count


def record_resident_answer(visit_id: str, answer: str) -> CareVisit | None:
    """The resident's answer is a second, independent fact. If it contradicts the camera,
    the visit becomes `unverified` and both facts stay visible."""
    from guardian.engine import incidents

    with db.session() as s:
        visit = s.get(CareVisit, visit_id)
        if visit is None:
            return None
        visit.resident_answer = answer
        camera_saw_arrival = visit.arrived_at is not None
        if (camera_saw_arrival and answer == "no") or (not camera_saw_arrival and answer == "yes"):
            visit.status = "unverified"
        s.add(visit)
        s.commit()
    for inc in _no_show_incidents():
        if any(e.payload.get("visit_id") == visit_id for e in incidents.evidence(inc.id)):
            said = "came" if answer == "yes" else "did not come"
            incidents.add_evidence(inc.id, "human_decision", f"Resident says the caregiver {said}", {"visit_id": visit_id, "resident_answer": answer})
    bus.publish("care", {"id": visit_id})
    return visit


def _no_show_incidents():
    from guardian.models import Incident

    with db.session() as s:
        return list(s.exec(select(Incident).where(Incident.playbook_id == "caregiver_no_show")).all())


async def run_forever() -> None:
    while True:
        try:
            check()
        except Exception:
            log.exception("care visit check failed")
        await asyncio.sleep(60 / clock.speed())
