"""Periodic device checks:
- thermostat temperature bands → temperature.low / temperature.high / temperature.normal (on change)
- safety-critical devices that stop reporting → device.stale (once, until they report again)
"""

import asyncio
import logging

from guardian import clock
from guardian.config import settings
from guardian.devices import registry, virtual
from guardian.engine.playbooks import SAFETY_CRITICAL_KINDS

log = logging.getLogger("guardian.monitor")


def check_temperatures() -> None:
    t = virtual.thermostat()
    if t is None:
        return
    dev = registry.get(t.device_id)
    reading = t.read_state()
    c = reading["current_c"]
    band = "low" if c < settings.temp_low_c else "high" if c > settings.temp_high_c else "normal"
    if dev.state.get("band", "normal") != band:
        limit = settings.temp_low_c if band == "low" else settings.temp_high_c
        registry.record_signal(dev, f"temperature.{band}", value={"current_c": c, "limit_c": limit, "band": band})
        registry.record_signal(dev, "temperature.reading", value={"band": band, "current_c": c})


def check_stale() -> None:
    for dev in registry.all_devices():
        if dev.source == "virtual" and (v := virtual.get_virtual(dev.id)) is not None:
            v.read_state()  # simulated devices are always reachable: heartbeat them
            dev = registry.get(dev.id)
        if dev.kind not in SAFETY_CRITICAL_KINDS or dev.state.get("stale"):
            continue
        usable, problems = registry.reliability(dev)
        if any("not reported" in p for p in problems):
            registry.record_signal(dev, "device.stale", value={"problems": problems}, touch=False)


async def run_forever() -> None:
    while True:
        try:
            check_temperatures()
            check_stale()
        except Exception:
            log.exception("device monitor failed")
        await asyncio.sleep(30 / clock.speed())
