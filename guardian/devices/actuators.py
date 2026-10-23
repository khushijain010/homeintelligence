"""Actuators. The only entry point is `execute`, which refuses anything the policy engine
did not allow. Nothing else in Guardian may touch a device that changes the world.
Where a device can be read back, the outcome carries `verified` from that readback."""

from dataclasses import dataclass, field
from typing import Any

import httpx

from guardian.bus import bus
from guardian.config import settings
from guardian.devices import virtual
from guardian.engine.policy import Decision
from guardian.models import Device
from guardian.ring.client import get_client


class PolicyViolation(RuntimeError):
    pass


@dataclass
class Outcome:
    result: str  # ok | failed | timeout
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    verified: str | None = None  # yes | no from an immediate readback, None = verify step decides


async def _valve_close(device: Device | None, params: dict) -> Outcome:
    valve = virtual.get_valve(device.id) if device else None
    if valve is None:
        return Outcome("failed", "valve not found")
    valve.close()
    return Outcome("ok", "close command sent to valve")


async def _snapshot(device: Device | None, params: dict) -> Outcome:
    if device is None:
        return Outcome("failed", "no camera")
    try:
        snap = await get_client().snapshot_latest(device.external_id)
    except httpx.HTTPError as exc:
        return Outcome("failed", f"snapshot failed: {exc}")
    return Outcome("ok", f"snapshot from {device.name}", snap)


async def _notify(device: Device | None, params: dict) -> Outcome:
    from guardian.notify import sns

    bus.publish("notification", params)  # dashboard delivery
    data: dict[str, Any] = {}
    if params.get("critical") or params.get("decision") or params.get("digest"):
        message_id = sns.publish(params.get("role", "owner"), params.get("message", ""), params.get("incident_id"))
        if message_id:
            data["sns_message_id"] = message_id
    return Outcome("ok", f"notified {params.get('to', 'owner')}: {params.get('message', '')}", data)


async def _thermostat_set_heat(device: Device | None, params: dict) -> Outcome:
    t = virtual.get_virtual(device.id) if device else None
    if not isinstance(t, virtual.VirtualThermostat):
        return Outcome("failed", "thermostat not found")
    t.set_heat(params.get("setpoint_c", 16))
    return Outcome("ok", f"heating set to {params.get('setpoint_c', 16):g} °C", t.read_state())


async def _thermostat_fan_off(device: Device | None, params: dict) -> Outcome:
    t = virtual.get_virtual(device.id) if device else None
    if not isinstance(t, virtual.VirtualThermostat):
        return Outcome("failed", "thermostat not found")
    t.fan_off()
    state = t.read_state()
    return Outcome("ok", "HVAC fan switched off", state, verified="yes" if state["fan"] == "off" else "no")


async def _plug_off(device: Device | None, params: dict) -> Outcome:
    p = virtual.get_virtual(device.id) if device else None
    if not isinstance(p, virtual.VirtualPlug):
        return Outcome("failed", "plug not found")
    p.turn_off()
    state = p.read_state()
    return Outcome("ok", f"{device.name} switched off", state, verified="no" if state["on"] else "yes")


async def _chime_play(device: Device | None, params: dict) -> Outcome:
    if device is None:
        return Outcome("failed", "no chime")
    try:
        await get_client().chime_play(device.external_id, params.get("slot", settings.chime_warning_slot))
    except httpx.HTTPError as exc:
        return Outcome("failed", f"chime failed: {exc}")
    return Outcome("ok", f"warning tone played on {device.name}")


_HANDLERS = {
    "valve.close": _valve_close,
    "snapshot.capture": _snapshot,
    "notify.owner": _notify,
    "notify.family": _notify,
    "notify.neighbor": _notify,
    "notify.everyone": _notify,
    "thermostat.set_heat": _thermostat_set_heat,
    "thermostat.fan_off": _thermostat_fan_off,
    "plug.off": _plug_off,
    "chime.play": _chime_play,
}


async def execute(decision: Decision, action_type: str, device: Device | None, params: dict | None = None) -> Outcome:
    if decision.outcome != "allow":
        raise PolicyViolation(f"{action_type} not allowed by policy: {decision.reasons}")
    handler = _HANDLERS.get(action_type)
    if handler is None:
        raise PolicyViolation(f"no actuator for {action_type}")
    return await handler(device, params or {})
