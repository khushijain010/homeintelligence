"""14 days of back-dated household history, injected through ring-sandbox (`at` = the past).

Kamla: morning walk 06:40–07:30, milk delivery ~07:00, evening walk 17:00–18:00.
Sunita: the ring-sandbox `home_aide_visit` scenario at ~10:00, Mon–Sat.
Each injected event's webhook payload is fed straight into Guardian's normal webhook ingestion
(no network flood). Times jitter a few minutes per day, deterministically.
"""

import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from ring_sandbox import scenarios as ring_scenarios

from guardian import clock
from guardian.config import settings
from guardian.demo import seed
from guardian.ring import webhooks

DOORBELL, DOOR = "Front Door", "Front Door Sensor"
Event = tuple[datetime, str | None, str, str | None]  # (at, device name, ring event type, sub_type)


def _at(day: date, hh: int, mm: int, jitter_min: int, rng: random.Random) -> datetime:
    base = datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo(settings.home_timezone))
    return base + timedelta(minutes=rng.randint(-jitter_min, jitter_min), seconds=rng.randint(0, 59))


def _door_cycle(t: datetime, person_first: bool) -> list[Event]:
    person = (t - timedelta(seconds=20) if person_first else t + timedelta(seconds=25), DOORBELL, "motion_detected", "human")
    return [
        person,
        (t, DOOR, "contact_sensor_faulted", None),
        (t + timedelta(seconds=15), DOOR, "contact_sensor_cleared", None),
    ]


def day_episodes(day: date, caregiver_days: set[int], skip_care: bool = False) -> list[list[Event]]:
    """Episodes are kept whole: a walk is only seeded once both its departure and return happened."""
    rng = random.Random(day.toordinal())
    episodes: list[list[Event]] = []
    out, back = _at(day, 6, 40, 6, rng), _at(day, 7, 30, 6, rng)
    episodes.append(_door_cycle(out, person_first=True) + _door_cycle(back, person_first=False))
    milk = _at(day, 7, 0, 5, rng)
    episodes.append([(milk, DOORBELL, "motion_detected", "human"), (milk + timedelta(seconds=10), DOORBELL, "button_press", None)])
    if day.weekday() in caregiver_days and not skip_care:
        start = _at(day, 10, 3, 3, rng)
        visit = ring_scenarios.resolve("home_aide_visit")
        episodes.append([(start + timedelta(seconds=s.offset_s), s.device, s.type, s.sub_type) for s in visit.steps])
    out, back = _at(day, 17, 0, 8, rng), _at(day, 18, 0, 8, rng)
    episodes.append(_door_cycle(out, person_first=True) + _door_cycle(back, person_first=False))
    return episodes


def plan(
    days: int = 14,
    include_today: bool = True,
    now: datetime | None = None,
    caregiver_days: set[int] | None = None,
    skip_care_day: date | None = None,
) -> list[Event]:
    now = now or clock.now()
    today = now.astimezone(ZoneInfo(settings.home_timezone)).date()
    caregiver_days = caregiver_days if caregiver_days is not None else {0, 1, 2, 3, 4, 5}
    events: list[Event] = []
    for i in range(days, -1 if include_today else 0, -1):
        day = today - timedelta(days=i)
        for ep in day_episodes(day, caregiver_days, skip_care=day == skip_care_day):
            if max(e[0] for e in ep) < now:
                events.extend(ep)
    return sorted(events, key=lambda e: e[0])


async def seed_history(
    control: httpx.AsyncClient, days: int = 14, include_today: bool = True, skip_care_day: date | None = None
) -> int:
    events = plan(days, include_today, skip_care_day=skip_care_day)
    for at, device, event_type, sub_type in events:
        body = {
            "device_id": seed.DEVICE_IDS[device] if device else None,
            "type": event_type,
            "sub_type": sub_type,
            "at": at.isoformat(),
            "deliver": False,
        }
        resp = await control.post("/_sandbox/events", json=body)
        resp.raise_for_status()
        webhooks.ingest_payload(resp.json()["webhook"])
    return len(events)
