"""Post-action readback. A result we cannot confirm is a failure, never a success.
Waits use clock time, so demo speed-ups shorten them."""

import asyncio

from guardian import clock
from guardian.config import settings
from guardian.devices import registry, virtual
from guardian.engine import incidents
from guardian.models import Action

POLL = 0.2
RISE_C = 0.5  # minimum rise that counts as "heating works"


def wet_sensors() -> set[str]:
    return {d.id for d in registry.all_devices("flood_sensor") if d.state.get("wet")}


async def valve_closed_and_dry(action: Action, wet_at_action: set[str]) -> tuple[str, str]:
    """Returns (verified yes|no, result ok|failed|timeout) and writes verification evidence."""
    iid = action.incident_id
    valve = virtual.get_valve(action.target_device_id or "")
    if valve is None:
        incidents.add_evidence(iid, "verification", "Valve not found for readback", {"check": "valve_readback"})
        return "no", "failed"

    # 1. readback: valve reports closed
    timer = clock.Timer(settings.valve_close_seconds + 5)
    position = valve.read_state()
    while position not in ("closed", "fault") and not timer.expired():
        await asyncio.sleep(POLL)
        position = valve.read_state()
    if position != "closed":
        result = "failed" if position == "fault" else "timeout"
        incidents.add_evidence(
            iid,
            "verification",
            f"Valve readback: {position} — could not confirm the valve is closed",
            {"check": "valve_readback", "position": position, "passed": False},
        )
        return "no", result
    incidents.add_evidence(
        iid, "verification", "Valve readback: closed", {"check": "valve_readback", "position": position, "passed": True}
    )

    # 2. no new flood sensor turns wet during the quiet window
    timer = clock.Timer(settings.verify_quiet_seconds)
    while not timer.expired():
        new = wet_sensors() - wet_at_action
        if new:
            names = ", ".join(registry.get(d).name for d in new)
            incidents.add_evidence(
                iid,
                "verification",
                f"New sensor wet after valve closed: {names}",
                {"check": "quiet_window", "new_wet": sorted(new), "passed": False},
            )
            return "no", "ok"
        await asyncio.sleep(POLL)
    incidents.add_evidence(
        iid,
        "verification",
        f"No new flood sensor turned wet for {settings.verify_quiet_seconds:g} s",
        {"check": "quiet_window", "seconds": settings.verify_quiet_seconds, "passed": True},
    )
    return "yes", "ok"


async def temperature_rising(action: Action, temp_at_action: float | None) -> tuple[str, str]:
    """Freeze protection: thermostat reads back mode heat, then the temperature rises within the window."""
    iid = action.incident_id
    t = virtual.get_virtual(action.target_device_id or "")
    if not isinstance(t, virtual.VirtualThermostat):
        incidents.add_evidence(iid, "verification", "Thermostat not found for readback", {"check": "thermostat_readback"})
        return "no", "failed"
    state = t.read_state()
    if state["mode"] != "heat":
        incidents.add_evidence(iid, "verification", f"Thermostat readback: mode {state['mode']}, not heating", {"check": "thermostat_readback", "passed": False})
        return "no", "failed"
    incidents.add_evidence(
        iid, "verification", f"Thermostat readback: heating to {state['setpoint_c']:g} °C", {"check": "thermostat_readback", "passed": True, **state}
    )
    start = temp_at_action if temp_at_action is not None else state["current_c"]
    timer = clock.Timer(settings.temp_verify_minutes * 60)
    while not timer.expired():
        now_c = t.read_state()["current_c"]
        if now_c >= start + RISE_C:
            incidents.add_evidence(
                iid,
                "verification",
                f"Temperature rising: {start:.1f} °C → {now_c:.1f} °C",
                {"check": "temperature_rising", "from": start, "to": now_c, "passed": True},
            )
            return "yes", "ok"
        await asyncio.sleep(POLL)
    incidents.add_evidence(
        iid,
        "verification",
        f"Temperature did not rise within {settings.temp_verify_minutes:g} min",
        {"check": "temperature_rising", "from": start, "passed": False},
    )
    return "no", "timeout"
