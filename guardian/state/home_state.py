"""Mode + occupancy. `unknown` must be treated as possibly occupied for safety decisions."""

from datetime import timedelta
from typing import Any

from sqlmodel import select

from guardian import clock, db
from guardian.bus import bus
from guardian.models import Device, Home, Signal

ACTIVITY_TYPES = ("motion.human", "contact.opened", "contact.closed")
ACTIVITY_WINDOW = timedelta(minutes=30)


def get_home() -> Home:
    with db.session() as s:
        home = s.exec(select(Home)).first()
        if home is None:
            home = Home(name="Home")
            s.add(home)
            s.commit()
        return home


def set_mode(mode: str) -> Home:
    if mode not in ("home", "away", "night"):
        raise ValueError(f"bad mode {mode!r}")
    with db.session() as s:
        home = s.exec(select(Home)).first() or Home(name="Home")
        home.mode = mode
        s.add(home)
        s.commit()
    bus.publish("home", {"mode": mode})
    return home


def snapshot() -> dict[str, Any]:
    home = get_home()
    if home.mode == "away":
        occupancy, confidence = "empty", 0.8
        reasons = ["Mode is Away"]
        since = clock.now() - ACTIVITY_WINDOW
        with db.session() as s:
            recent = s.exec(
                select(Signal, Device)
                .where(Signal.device_id == Device.id)
                .where(Signal.type.in_(ACTIVITY_TYPES))  # type: ignore[attr-defined]
                .where(Signal.observed_at >= since)
                .order_by(Signal.observed_at.desc())  # type: ignore[attr-defined]
            ).first()
        if recent:
            sig, dev = recent
            occupancy, confidence = "unknown", 0.5
            reasons.append(f"{sig.type} on {dev.name} in the last 30 min")
    else:
        occupancy, confidence = "occupied", 0.8
        reasons = [f"Mode is {home.mode.capitalize()}"]
    return {
        "mode": home.mode,
        "occupancy": occupancy,
        "occupancy_confidence": confidence,
        "reasons": reasons,
    }
