"""Sensors have no history endpoint, so Guardian polls status and emits a Signal on change.

The same sync also creates Device rows for Ring devices Guardian has not seen yet.
"""

import asyncio
import logging

import httpx

from guardian import clock, db
from guardian.config import settings
from guardian.devices import registry
from guardian.models import Device
from guardian.ring import normalize
from guardian.ring.client import get_client

log = logging.getLogger("guardian.poller")

# room per Ring device name (Ring has no rooms); set by the demo seed
ROOMS: dict[str, str] = {}


async def sync_once() -> None:
    for item in await get_client().devices():
        dev_doc, status = item["device"], item["status"]
        kind = normalize.device_kind(dev_doc, status, item["capabilities"])
        if kind is None:
            continue
        name = dev_doc["attributes"]["name"]
        state = normalize.sensor_state(kind, status)
        reported = normalize.reported_at(status)
        device = registry.by_external_id(dev_doc["id"])
        if device and reported and device.last_reported_at and reported <= clock.as_utc(device.last_reported_at):
            continue  # nothing newer than what webhooks already told us
        with db.session() as s:
            if device is None:
                device = Device(
                    source="ring",
                    external_id=dev_doc["id"],
                    kind=kind,
                    name=name,
                    room=ROOMS.get(name, ""),
                    state=state,
                )
                changed = {}
            else:
                device = s.get(Device, device.id)
                changed = {k: v for k, v in state.items() if device.state.get(k) != v}
            device.online = bool(status.get("online", True))
            device.battery_pct = normalize.battery_pct(status)
            device.last_reported_at = reported or device.last_reported_at
            s.add(device)
            s.commit()
        for key, val in changed.items():
            sig_type = {
                ("wet", True): "water.detected",
                ("wet", False): "water.cleared",
                ("open", True): "contact.opened",
                ("open", False): "contact.closed",
            }[(key, val)]
            registry.record_signal(device, sig_type, value={"via": "poll"})


async def run_forever() -> None:
    backoff = 1.0
    while True:
        try:
            await sync_once()
            backoff = 1.0
            await asyncio.sleep(settings.ring_poll_seconds)
        except httpx.HTTPError as exc:
            log.warning("ring poll failed (%s); retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
