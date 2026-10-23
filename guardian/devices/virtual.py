"""Simulated devices (always labelled Simulated in the UI):

- water shut-off valve: open -> closing -> closed (or fault)
- thermostat: mode, setpoint, current temperature drifting toward the setpoint / ambient
- smart plug for a listed heater
- smoke/CO alarm
"""

import asyncio
from typing import Any

from guardian import clock, db
from guardian.config import settings
from guardian.devices import registry
from guardian.models import Device

VALVE_EXTERNAL_ID = "virtual.valve.main"
THERMOSTAT_EXTERNAL_ID = "virtual.thermostat.main"
PLUG_EXTERNAL_ID = "virtual.plug.heater"
SMOKE_EXTERNAL_ID = "virtual.smoke.hall"

HEAT_RATE_C_PER_MIN = 1.0  # heating, simulated (fast so the demo shows a rise within a minute)
DRIFT_RATE_C_PER_MIN = 0.2  # passive drift toward ambient when not heating


class VirtualDevice:
    def __init__(self, device_id: str):
        self.device_id = device_id

    def _touch(self) -> None:
        """A read is also a health report from the device."""
        with db.session() as s:
            dev = s.get(Device, self.device_id)
            dev.last_reported_at = clock.now()
            s.add(dev)
            s.commit()

    def _emit(self, signal_type: str, value: dict[str, Any]) -> None:
        registry.record_signal(registry.get(self.device_id), signal_type, value=value)

    def cancel(self) -> None:
        pass


class VirtualValve(VirtualDevice):
    def __init__(self, device_id: str):
        super().__init__(device_id)
        self.position = "open"
        self.fault_on_close = False  # demo flag: the motor jams while closing
        self._task: asyncio.Task | None = None

    def close(self) -> None:
        if self.position in ("closing", "closed", "fault"):
            return
        self.position = "closing"
        self._emit("valve.state", {"position": self.position})
        self._task = asyncio.create_task(self._finish_close())

    async def _finish_close(self) -> None:
        await clock.sleep(settings.valve_close_seconds)
        self.position = "fault" if self.fault_on_close else "closed"
        self._emit("valve.state", {"position": self.position})

    def read_state(self) -> str:
        self._touch()
        return self.position

    def set_fault(self) -> None:
        self.position = "fault"
        self._emit("valve.state", {"position": self.position})

    def reset(self) -> None:
        self.cancel()
        self.position, self.fault_on_close = "open", False
        self._emit("valve.state", {"position": self.position})

    def cancel(self) -> None:
        if self._task:
            self._task.cancel()


class VirtualThermostat(VirtualDevice):
    """Current temperature integrates over clock time, so demo speed-ups heat faster too."""

    def __init__(self, device_id: str, current_c: float = 24.0, ambient_c: float = 24.0):
        super().__init__(device_id)
        self.mode, self.setpoint_c, self.fan = "off", 22.0, "auto"
        self.ambient_c = ambient_c
        self._temp, self._at = current_c, clock.now()

    def current(self) -> float:
        minutes = max((clock.now() - self._at).total_seconds() / 60, 0)
        temp = self._temp
        if self.mode == "heat" and self.setpoint_c > temp:
            return min(self.setpoint_c, temp + HEAT_RATE_C_PER_MIN * minutes)
        step = DRIFT_RATE_C_PER_MIN * minutes
        return min(self.ambient_c, temp + step) if self.ambient_c > temp else max(self.ambient_c, temp - step)

    def _rebase(self) -> None:
        self._temp, self._at = self.current(), clock.now()

    def read_state(self) -> dict[str, Any]:
        self._touch()
        return {"mode": self.mode, "setpoint_c": self.setpoint_c, "current_c": round(self.current(), 1), "fan": self.fan}

    def set_heat(self, setpoint_c: float) -> None:
        self._rebase()
        self.mode, self.setpoint_c = "heat", float(setpoint_c)
        self._emit("thermostat.state", self.read_state())

    def fan_off(self) -> None:
        self.fan = "off"
        self._emit("thermostat.state", self.read_state())

    def simulate(self, current_c: float, ambient_c: float) -> None:
        self.mode, self.fan, self._temp, self._at, self.ambient_c = "off", "auto", current_c, clock.now(), ambient_c
        self._emit("thermostat.state", self.read_state())


class VirtualPlug(VirtualDevice):
    def __init__(self, device_id: str):
        super().__init__(device_id)
        self.on = True

    def turn_off(self) -> None:
        self.on = False
        self._emit("plug.state", {"on": False})

    def read_state(self) -> dict[str, Any]:
        self._touch()
        return {"on": self.on}


class VirtualSmokeAlarm(VirtualDevice):
    def __init__(self, device_id: str):
        super().__init__(device_id)
        self.alarm = "none"  # none | smoke | co

    def trigger(self, what: str) -> None:
        self.alarm = what
        self._emit(f"{what}.detected", {"alarm": what})

    def clear(self) -> None:
        self.alarm = "none"
        self._emit("smoke.cleared", {"alarm": "none"})

    def read_state(self) -> dict[str, Any]:
        self._touch()
        return {"alarm": self.alarm}


_devices: dict[str, VirtualDevice] = {}

SPECS = {
    VALVE_EXTERNAL_ID: ("valve", "Main Water Valve", "Utility", VirtualValve, {"position": "open"}),
    THERMOSTAT_EXTERNAL_ID: ("thermostat", "Living Room Thermostat", "Living Room", VirtualThermostat, {"mode": "off"}),
    PLUG_EXTERNAL_ID: ("plug", "Room Heater Plug", "Bedroom", VirtualPlug, {"on": True}),
    SMOKE_EXTERNAL_ID: ("smoke_alarm", "Hall Smoke/CO Alarm", "Hall", VirtualSmokeAlarm, {"alarm": "none"}),
}


def _ensure(external_id: str) -> VirtualDevice:
    kind, name, room, cls, state = SPECS[external_id]
    dev = registry.by_external_id(external_id)
    if dev is None:
        dev = Device(
            source="virtual",
            external_id=external_id,
            kind=kind,
            name=name,
            room=room,
            is_actuator=kind != "smoke_alarm",
            state={**state, "simulated": True},
            last_reported_at=clock.now(),
        )
        with db.session() as s:
            s.add(dev)
            s.commit()
    if dev.id not in _devices:
        obj = cls(dev.id)
        if isinstance(obj, VirtualValve):
            obj.position = dev.state.get("position", "open")  # survive restarts
        _devices[dev.id] = obj
    return _devices[dev.id]


def ensure_valve() -> VirtualValve:
    return _ensure(VALVE_EXTERNAL_ID)  # type: ignore[return-value]


def ensure_all() -> None:
    for external_id in SPECS:
        _ensure(external_id)


def get_virtual(device_id: str) -> VirtualDevice | None:
    return _devices.get(device_id)


def get_valve(device_id: str) -> VirtualValve | None:
    dev = _devices.get(device_id)
    return dev if isinstance(dev, VirtualValve) else None


def thermostat() -> VirtualThermostat | None:
    return next((d for d in _devices.values() if isinstance(d, VirtualThermostat)), None)


def smoke_alarm() -> VirtualSmokeAlarm | None:
    return next((d for d in _devices.values() if isinstance(d, VirtualSmokeAlarm)), None)


def forget_all() -> None:
    for d in _devices.values():
        d.cancel()
    _devices.clear()
