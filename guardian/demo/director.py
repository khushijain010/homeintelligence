"""Demo scenarios. Ring events are injected into the emulator, which signs and delivers the
webhooks to Guardian exactly as Ring would."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from ring_sandbox import scenarios as ring_scenarios
from sqlmodel import select

from guardian import clock, db
from guardian.config import settings
from guardian.demo import history, seed
from guardian.devices import monitor, virtual
from guardian.engine import runner
from guardian.models import CareSchedule
from guardian.ring import poller
from guardian.state import care_visits, home_state, routine

log = logging.getLogger("guardian.demo")

SEED_HISTORY = True  # tests switch this off unless they need the routine baseline
LEAK_GAP_SECONDS = 10.0  # Kitchen wet, then Bathroom wet
ENTRY_GAP_SECONDS = 2.0  # door opens, then person on camera

# Tests set this to post webhook payloads straight to Guardian instead of letting
# the emulator deliver them over the network.
post_webhook: Callable[[dict[str, Any]], Awaitable[None]] | None = None
_control: httpx.AsyncClient | None = None
_current: asyncio.Task | None = None


def control() -> httpx.AsyncClient:
    global _control
    if _control is None:
        _control = httpx.AsyncClient(base_url=settings.ring_api_base_url, timeout=10.0)
    return _control


def set_control(client: httpx.AsyncClient | None) -> None:
    global _control
    _control = client


async def inject(device_name: str | None, event_type: str, sub_type: str | None = None, at: datetime | None = None) -> None:
    body: dict[str, Any] = {
        "device_id": seed.DEVICE_IDS[device_name] if device_name else None,
        "type": event_type,
        "sub_type": sub_type,
        "deliver": post_webhook is None,
    }
    if at is not None:
        body["at"] = at.isoformat()
    resp = await control().post("/_sandbox/events", json=body)
    resp.raise_for_status()
    if post_webhook is not None:
        await post_webhook(resp.json()["webhook"])


async def play(name: str, start: datetime) -> None:
    """Replay a ring-sandbox scenario (built-in or wheel example) with its virtual start pinned."""
    sc = ring_scenarios.resolve(name)
    for step in sorted(sc.steps, key=lambda s: s.offset_s):
        await inject(step.device, step.type, step.sub_type, at=start + timedelta(seconds=step.offset_s))


async def reset(include_today: bool = True, skip_care_day: date | None = None) -> None:
    """Clean home + 14 days of back-dated history (when SEED_HISTORY), baseline and past care visits."""
    global _current
    if _current and not _current.done():
        _current.cancel()
    runner.cancel_all()
    speed = clock.speed()
    clock.reset()
    clock.set_speed(speed)  # the chosen demo speed survives resets
    seed.seed_db()
    await seed.seed_emulator(control(), register_webhook=post_webhook is None)
    await poller.sync_once()
    if SEED_HISTORY:
        await history.seed_history(control(), include_today=include_today, skip_care_day=skip_care_day)
        routine.recompute()
        today = routine.local(clock.now()).date()
        days = [today - timedelta(days=i) for i in range(14, -1 if include_today else 0, -1)]
        care_visits.backfill([d for d in days if d != skip_care_day])
    care_visits.monitoring_since = clock.now()


# --------------------------------------------------------------------------- scenarios


async def leak_empty_home() -> None:
    home_state.set_mode("away")
    await inject("Kitchen Leak Sensor", "flood_detected")
    await asyncio.sleep(LEAK_GAP_SECONDS)
    await inject("Bathroom Leak Sensor", "flood_detected")


async def leak_valve_fault() -> None:
    valve = virtual.ensure_valve()  # created by the seed; returns the live instance
    valve.fault_on_close = True
    await leak_empty_home()


async def leak_single_sensor() -> None:
    home_state.set_mode("away")
    await inject("Kitchen Leak Sensor", "flood_detected")


async def night_entry() -> None:
    home_state.set_mode("night")
    await inject("Front Door Sensor", "contact_sensor_faulted")
    await asyncio.sleep(ENTRY_GAP_SECONDS)
    await inject("Front Door", "motion_detected", "human")


def _last_care_day(schedule: CareSchedule) -> date:
    """Most recent scheduled day whose window has already started (in real time)."""
    tz = ZoneInfo(settings.home_timezone)
    day = datetime.now(UTC).astimezone(tz).date()
    for _ in range(8):
        start, _ = care_visits.window(schedule, day)
        if day.weekday() in schedule.weekdays and start + timedelta(hours=1, minutes=5) <= datetime.now(UTC):
            return day
        day -= timedelta(days=1)
    raise RuntimeError("no scheduled care day found")


async def caregiver_no_show() -> None:
    with db.session() as s:
        schedule = s.exec(select(CareSchedule)).first()
    day = _last_care_day(schedule)
    start, _ = care_visits.window(schedule, day)
    # demo clock: jump to just before Sunita's window, replay ring-sandbox `no_show`
    # (a vehicle passes, nobody comes to the door), then to start + 61 min.
    care_visits.monitoring_since = start - timedelta(minutes=31)
    clock.set_now(start - timedelta(minutes=31))
    home_state.set_mode("home")
    await play("no_show", start + timedelta(minutes=10))
    clock.set_now(start + timedelta(minutes=61))
    care_visits.check()


async def missed_morning() -> None:
    """History has no activity today; jump to just past Kamla's usual first-activity time + 45 min."""
    b = routine.baseline()
    if b is None or b.first_activity_p90_min is None:
        raise RuntimeError("no routine baseline (SEED_HISTORY off?)")
    today = routine.local(clock.now()).date()
    home_state.set_mode("home")
    clock.set_now(routine.midnight(today) + timedelta(minutes=b.first_activity_p90_min + 46))
    await routine.check()


async def freeze_night() -> None:
    """Empty home on a freezing night: thermostat reads 3 °C, then the Kitchen sensor reports freeze."""
    home_state.set_mode("away")
    virtual.thermostat().simulate(current_c=3.0, ambient_c=1.0)
    monitor.check_temperatures()
    await asyncio.sleep(ENTRY_GAP_SECONDS)
    await inject("Kitchen Leak Sensor", "freeze_detected")


async def smoke_advisory() -> None:
    home_state.set_mode("home")
    virtual.smoke_alarm().trigger("smoke")


SCENARIOS: dict[str, Callable[[], Awaitable[None]]] = {
    "leak_empty_home": leak_empty_home,
    "leak_valve_fault": leak_valve_fault,
    "leak_single_sensor": leak_single_sensor,
    "night_entry": night_entry,
    "caregiver_no_show": caregiver_no_show,
    "missed_morning": missed_morning,
    "freeze_night": freeze_night,
    "smoke_advisory": smoke_advisory,
}


def _reset_options(name: str) -> dict:
    if name == "missed_morning":
        return {"include_today": False}
    if name == "caregiver_no_show":
        with db.session() as s:
            schedule = s.exec(select(CareSchedule)).first()
        return {"skip_care_day": _last_care_day(schedule)} if schedule else {}
    return {}


async def run(name: str) -> asyncio.Task:
    """Reset to a clean home, then play the scenario in the background."""
    global _current
    if name not in SCENARIOS:
        raise KeyError(name)
    await reset(**_reset_options(name))
    _current = asyncio.create_task(SCENARIOS[name]())
    return _current

