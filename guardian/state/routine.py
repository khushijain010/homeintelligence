"""Elder routine learning and deviation rules (Kamla).

Activity = front-door human motion, doorbell press, exterior door opened. Guardian has no indoor
sensors, so outings are inferred from exterior-door openings outside caregiver windows: the 1st,
3rd, … opening of a day is a departure, the next one the return.
"""

import asyncio
import logging
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import select

from guardian import clock, db
from guardian.config import settings
from guardian.devices import registry
from guardian.engine.playbooks import EXTERIOR_ROOMS
from guardian.models import CareSchedule, Device, MonitoringPause, RoutineBaseline, Signal

log = logging.getLogger("guardian.routine")

ACTIVITY_TYPES = ("motion.human", "doorbell.pressed", "contact.opened")
WINDOW_DAYS = 14
MIN_DAYS = 7
SLOTS = 48
MORNING_GRACE = timedelta(minutes=45)
NIGHT_RETURN = timedelta(minutes=20)
OUTING_GRACE = timedelta(minutes=90)
ROUTINE_EXTERNAL_ID = "guardian.routine"


def tz() -> ZoneInfo:
    return ZoneInfo(settings.home_timezone)


def local(dt: datetime) -> datetime:
    return clock.as_utc(dt).astimezone(tz())


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz())


def _hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_night(when: datetime) -> bool:
    t = local(when).time()
    start, end = _hhmm(settings.routine_night_start), _hhmm(settings.routine_night_end)
    return t >= start or t < end


def routine_device() -> Device:
    dev = registry.by_external_id(ROUTINE_EXTERNAL_ID)
    if dev is None:
        dev = Device(
            source="virtual",
            external_id=ROUTINE_EXTERNAL_ID,
            kind="routine",
            name="Routine monitor",
            last_reported_at=clock.now(),
        )
        with db.session() as s:
            s.add(dev)
            s.commit()
    return dev


def exterior_ids() -> list[str]:
    return [d.id for d in registry.all_devices() if d.room in EXTERIOR_ROOMS and d.kind in ("doorbell", "camera", "contact_sensor")]


def activity(start: datetime, end: datetime, types: tuple[str, ...] = ACTIVITY_TYPES) -> list[Signal]:
    ids = exterior_ids()
    if not ids:
        return []
    with db.session() as s:
        return list(
            s.exec(
                select(Signal)
                .where(Signal.type.in_(types))  # type: ignore[attr-defined]
                .where(Signal.device_id.in_(ids))  # type: ignore[attr-defined]
                .where(Signal.observed_at >= clock.as_utc(start))
                .where(Signal.observed_at < clock.as_utc(end))
                .order_by(Signal.observed_at)  # type: ignore[arg-type]
            ).all()
        )


def _care_schedules() -> list[CareSchedule]:
    with db.session() as s:
        return list(s.exec(select(CareSchedule)).all())


def in_care_window(when: datetime, schedules: list[CareSchedule] | None = None) -> bool:
    """Door activity around a scheduled caregiver visit is the caregiver's, not Kamla's."""
    t = local(when)
    for sch in schedules if schedules is not None else _care_schedules():
        if t.weekday() not in sch.weekdays:
            continue
        start = datetime.combine(t.date(), _hhmm(sch.start_local), tzinfo=tz()) - timedelta(minutes=30)
        end = datetime.combine(t.date(), _hhmm(sch.end_local), tzinfo=tz()) + timedelta(minutes=60)
        if start <= t <= end:
            return True
    return False


def outings(opens: list[datetime]) -> list[tuple[datetime, datetime | None]]:
    """Pair door openings (already filtered to Kamla's): (departure, return or None)."""
    pairs = []
    for i in range(0, len(opens), 2):
        pairs.append((opens[i], opens[i + 1] if i + 1 < len(opens) else None))
    return pairs


def percentile(values: list[int], p: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


# --------------------------------------------------------------------------- baseline


def recompute(now: datetime | None = None) -> RoutineBaseline:
    now = now or clock.now()
    today = local(now).date()
    days = [today - timedelta(days=i) for i in range(WINDOW_DAYS, 0, -1)]
    signals = activity(midnight(days[0]), midnight(today))
    schedules = _care_schedules()
    by_day: dict[date, list[Signal]] = defaultdict(list)
    for sig in signals:
        by_day[local(sig.observed_at).date()].append(sig)
    learned = [d for d in days if by_day[d]]

    slots: dict[str, list[float]] = {}
    observed_days = [d for d in days if learned and d >= learned[0]]  # days since data began
    for w in range(7):
        matching = [d for d in observed_days if d.weekday() == w]
        counts = [0] * SLOTS
        for d in matching:
            hit = {(local(s.observed_at).hour * 60 + local(s.observed_at).minute) // 30 for s in by_day[d]}
            for slot in hit:
                counts[slot] += 1
        slots[str(w)] = [round(c / len(matching), 3) if matching else 0.0 for c in counts]

    firsts = [local(by_day[d][0].observed_at) for d in learned]
    p90 = percentile([t.hour * 60 + t.minute for t in firsts], 0.9) if firsts else None

    longest = None
    for d in learned:
        opens = [local(s.observed_at) for s in by_day[d] if s.type == "contact.opened" and not in_care_window(s.observed_at, schedules)]
        for out, back in outings(opens):
            if back is not None and timedelta(minutes=5) <= back - out <= timedelta(hours=8):
                minutes = int((back - out).total_seconds() // 60)
                longest = max(longest or 0, minutes)

    baseline = RoutineBaseline(
        computed_at=now,
        learned_days=len(learned),
        slots=slots,
        first_activity_p90_min=p90,
        longest_outing_min=longest,
        daily_counts={d.isoformat(): len(by_day[d]) for d in days},
    )
    with db.session() as s:
        for old in s.exec(select(RoutineBaseline)).all():
            s.delete(old)
        s.add(baseline)
        s.commit()
    return baseline


def baseline() -> RoutineBaseline | None:
    with db.session() as s:
        return s.exec(select(RoutineBaseline)).first()


def learning_status() -> dict[str, Any]:
    b = baseline()
    n = b.learned_days if b else 0
    return {"learned_days": n, "ready": n >= MIN_DAYS, "label": "Routine learned" if n >= MIN_DAYS else f"Learning routine {n}/{MIN_DAYS}"}


# --------------------------------------------------------------------------- pauses


def active_pause(now: datetime | None = None) -> MonitoringPause | None:
    now = now or clock.now()
    with db.session() as s:
        for p in s.exec(select(MonitoringPause).where(MonitoringPause.ended_early == False)).all():  # noqa: E712
            if clock.as_utc(p.until) > now:
                return p
    return None


def pause(hours: int, by: str = "resident") -> MonitoringPause:
    p = MonitoringPause(until=clock.now() + timedelta(hours=hours), set_by=by)
    with db.session() as s:
        s.add(p)
        s.commit()
    return p


def resume() -> None:
    with db.session() as s:
        for p in s.exec(select(MonitoringPause).where(MonitoringPause.ended_early == False)).all():  # noqa: E712
            if clock.as_utc(p.until) > clock.now():
                p.ended_early = True
                s.add(p)
        s.commit()


# --------------------------------------------------------------------------- deviations


def _already(key: str) -> bool:
    with db.session() as s:
        rows = s.exec(select(Signal).where(Signal.type == "routine.deviation")).all()
    return any(r.value.get("key") == key for r in rows)


def last_activity(before: datetime | None = None) -> Signal | None:
    before = before or clock.now()
    rows = activity(before - timedelta(days=30), before)
    return rows[-1] if rows else None


def describe_last_activity(before: datetime | None = None) -> str:
    sig = last_activity(before)
    if sig is None:
        return "no front-door activity recorded"
    dev = registry.get(sig.device_id)
    when = local(sig.observed_at)
    day = "today" if when.date() == local(before or clock.now()).date() else when.strftime("%d %b")
    verb = {"motion.human": "person seen at", "doorbell.pressed": "doorbell pressed at", "contact.opened": "door opened at"}[sig.type]
    return f"{verb} {dev.name if dev else 'front door'}, {when.strftime('%H:%M')} {day}"


def evaluate(now: datetime) -> list[dict[str, Any]]:
    """All deviations true at `now` (not yet de-duplicated)."""
    from guardian.state import care_visits  # local import: care_visits imports the engine

    b = baseline()
    if b is None or b.learned_days < MIN_DAYS or active_pause(now) or care_visits.visit_in_progress(now):
        return []
    today = local(now).date()
    day_start = midnight(today) + timedelta(hours=3)
    found: list[dict[str, Any]] = []

    if b.first_activity_p90_min is not None:
        deadline = midnight(today) + timedelta(minutes=b.first_activity_p90_min) + MORNING_GRACE
        if now >= deadline and not activity(day_start, deadline):
            found.append(
                {
                    "rule": "no_morning_activity",
                    "key": f"no_morning_activity:{today}",
                    "reason": f"No front-door activity by {local(deadline).strftime('%H:%M')} (usually active by "
                    f"{(midnight(today) + timedelta(minutes=b.first_activity_p90_min)).strftime('%H:%M')})",
                }
            )

    for sig in activity(now - timedelta(hours=24), now - NIGHT_RETURN, ("contact.opened",)):
        t = clock.as_utc(sig.observed_at)
        if is_night(t) and not activity(t + timedelta(seconds=90), t + NIGHT_RETURN):
            found.append(
                {
                    "rule": "night_exit",
                    "key": f"night_exit:{sig.id}",
                    "reason": f"Front door opened at {local(t).strftime('%H:%M')} at night; no activity back inside within 20 min",
                }
            )

    if b.longest_outing_min:
        opens = [s for s in activity(day_start, now, ("contact.opened",)) if not in_care_window(s.observed_at)]
        if len(opens) % 2 == 1:
            left = clock.as_utc(opens[-1].observed_at)
            limit = timedelta(minutes=b.longest_outing_min) + OUTING_GRACE
            if now - left > limit:
                found.append(
                    {
                        "rule": "not_returned",
                        "key": f"not_returned:{opens[-1].id}",
                        "reason": f"Went out at {local(left).strftime('%H:%M')} and has not returned; longest usual outing is "
                        f"{b.longest_outing_min} min",
                    }
                )

    counts = []
    for i in range(1, 22):
        d = today - timedelta(days=i)
        counts.append(len(activity(midnight(d), midnight(d + timedelta(days=1)))))
    recent, prior = counts[:7], counts[7:]
    if all(c > 0 for c in prior[-3:]) and sum(prior):  # needs data reaching back 21 days
        avg7, avg14 = sum(recent) / 7, sum(prior) / 14
        if avg7 < 0.6 * avg14:
            found.append(
                {
                    "rule": "low_activity_trend",
                    "key": f"low_activity_trend:{today}",
                    "reason": f"Front-door activity averaged {avg7:.1f}/day this week vs {avg14:.1f}/day the two weeks before",
                }
            )
    return found


async def check(now: datetime | None = None) -> list[Signal]:
    """Emit each new deviation once as a `routine.deviation` signal (the correlator opens incidents)."""
    now = now or clock.now()
    emitted = []
    for dev in evaluate(now):
        if _already(dev["key"]):
            continue
        dev["last_activity"] = describe_last_activity(now)
        sig = registry.record_signal(routine_device(), "routine.deviation", sub_type=dev["rule"], value=dev, observed_at=now)
        if sig:
            emitted.append(sig)
            if dev["rule"] == "low_activity_trend":
                await _family_digest(dev)
    return emitted


async def _family_digest(dev: dict[str, Any]) -> None:
    """Trend changes go to family as a digest, not an incident. Still policy-gated."""
    from guardian.devices import actuators
    from guardian.engine import policy

    decision = policy.evaluate(policy.PolicyContext(action_type="notify.family"))
    if decision.outcome == "allow":
        await actuators.execute(
            decision, "notify.family", None, {"to": "family", "message": f"Weekly routine note: {dev['reason']}.", "digest": True}
        )


async def run_forever() -> None:
    last_recompute: date | None = None
    while True:
        try:
            now = clock.now()
            if local(now).hour >= 3 and last_recompute != local(now).date():
                recompute(now)
                last_recompute = local(now).date()
            await check(now)
        except Exception:
            log.exception("routine check failed")
        await asyncio.sleep(60 / clock.speed())
