"""now() wrapper so tests can freeze time and demos can jump or speed up time.

All runner, verifier and check-in timers use Timer/sleep below, so a 10× or 60× demo clock
shortens them too. A frozen clock (tests) never advances, so timers then fall back to real time.
"""

import asyncio
import time as _time
from datetime import UTC, datetime, timedelta

_anchor_clock = datetime.now(UTC)  # clock reading at the anchor
_anchor_real = _anchor_clock  # wall-clock time at the anchor
_speed = 1.0
_frozen: datetime | None = None


def _real() -> datetime:
    return datetime.now(UTC)


def now() -> datetime:
    if _frozen is not None:
        return _frozen
    return _anchor_clock + (_real() - _anchor_real) * _speed


def set_now(when: datetime) -> None:
    """Jump the clock to `when`; it keeps ticking at the current speed (demo)."""
    global _anchor_clock, _anchor_real, _frozen
    _frozen = None
    _anchor_clock, _anchor_real = when.astimezone(UTC), _real()


def set_speed(multiplier: float) -> None:
    global _anchor_clock, _anchor_real, _speed
    _anchor_clock, _anchor_real = now(), _real()
    _speed = float(multiplier)


def speed() -> float:
    return _speed


def freeze(when: datetime) -> None:
    """Stop the clock at `when` (tests)."""
    global _frozen
    _frozen = when.astimezone(UTC)


def reset() -> None:
    global _anchor_clock, _anchor_real, _speed, _frozen
    _anchor_clock = _anchor_real = _real()
    _speed, _frozen = 1.0, None


def as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; every stored timestamp is UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Timer:
    """Deadline measured in clock time (honours demo speed)."""

    def __init__(self, seconds: float, start: datetime | None = None):
        self.seconds = seconds
        self.start = start or now()
        self._real_start = _time.monotonic()

    @property
    def deadline(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds)

    def remaining(self) -> float:
        if _frozen is not None:
            return self.seconds - (_time.monotonic() - self._real_start)
        return (self.deadline - now()).total_seconds()

    def expired(self) -> bool:
        return self.remaining() <= 0


async def sleep(seconds: float) -> None:
    t = Timer(seconds)
    while not t.expired():
        await asyncio.sleep(max(0.01, min(0.2, t.remaining() / max(_speed, 1.0))))
