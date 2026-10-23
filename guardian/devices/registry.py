"""Device lookups, reliability, and the one place signals are stored and published."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from guardian import clock, db, ledger
from guardian.bus import bus
from guardian.config import settings
from guardian.models import Device, Signal

# signal type -> (state key, value) applied to Device.state
_STATE_EFFECTS = {
    "water.detected": ("wet", True),
    "water.cleared": ("wet", False),
    "contact.opened": ("open", True),
    "contact.closed": ("open", False),
    "device.tamper": ("tamper", True),
    "device.tamper_cleared": ("tamper", False),
    "temperature.freeze": ("freeze", True),
    "temperature.freeze_cleared": ("freeze", False),
    "smoke.detected": ("alarm", "smoke"),
    "co.detected": ("alarm", "co"),
    "smoke.cleared": ("alarm", "none"),
}
# signals whose value is merged into Device.state
_MERGE_VALUE = {"valve.state", "thermostat.state", "plug.state", "temperature.reading"}


def by_external_id(external_id: str) -> Device | None:
    with db.session() as s:
        return s.exec(select(Device).where(Device.external_id == external_id)).first()


def get(device_id: str) -> Device | None:
    with db.session() as s:
        return s.get(Device, device_id)


def all_devices(kind: str | None = None) -> list[Device]:
    with db.session() as s:
        q = select(Device)
        if kind:
            q = q.where(Device.kind == kind)
        return list(s.exec(q).all())


def reliability(device: Device) -> tuple[bool, list[str]]:
    """Usable only if online, reported within SENSOR_STALE_MINUTES, battery >= 10%."""
    problems = []
    if not device.online:
        problems.append(f"{device.name} is offline")
    if device.last_reported_at is None:
        problems.append(f"{device.name} has never reported")
    elif clock.now() - clock.as_utc(device.last_reported_at) > timedelta(minutes=settings.sensor_stale_minutes):
        problems.append(f"{device.name} has not reported for over {settings.sensor_stale_minutes} min")
    if device.battery_pct is not None and device.battery_pct < 10:
        problems.append(f"{device.name} battery at {device.battery_pct}%")
    return (not problems, problems)


def record_signal(
    device: Device,
    type: str,
    *,
    sub_type: str | None = None,
    value: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    request_id: str | None = None,
    raw: dict[str, Any] | None = None,
    touch: bool = True,
) -> Signal | None:
    """Save a signal, update device health/state, publish it. Returns None on duplicate request id.

    touch=False for signals Guardian derives itself (e.g. device.stale): they are not a report
    from the device, so they must not refresh its health."""
    observed_at = observed_at or clock.now()
    sig = Signal(
        device_id=device.id,
        type=type,
        sub_type=sub_type,
        value=value or {},
        observed_at=observed_at,
        source_request_id=request_id,
        raw=raw or {},
    )
    with db.session() as s:
        dev = s.get(Device, device.id)
        s.add(sig)
        if type in ("device.online", "device.offline"):
            dev.online = type == "device.online"
        if type in _STATE_EFFECTS:
            key, val = _STATE_EFFECTS[type]
            dev.state = {**dev.state, key: val}
        if type in _MERGE_VALUE:
            dev.state = {**dev.state, **(value or {})}
        if type == "device.stale":
            dev.state = {**dev.state, "stale": True}
        last = clock.as_utc(dev.last_reported_at) if dev.last_reported_at else None
        if touch and (last is None or clock.as_utc(observed_at) > last):
            dev.last_reported_at = observed_at
            if dev.state.get("stale"):
                dev.state = {**dev.state, "stale": False}
        s.add(dev)
        try:
            s.commit()
        except IntegrityError:  # source_request_id already seen
            s.rollback()
            return None
    ledger.append(
        "signal",
        {"signal_id": sig.id, "device": device.name, "type": type, "sub_type": sub_type, "value": value or {}, "observed_at": observed_at},
    )
    bus.publish("signal", {"id": sig.id, "type": type, "device_id": device.id})
    bus.publish("device", {"id": device.id})
    return sig
