"""Demo home: Sharma Residence. Ring devices go into the emulator via /_sandbox (shapes in
docs/ring-api-notes.md); the valve is virtual."""

import httpx
from sqlmodel import select

from guardian import db
from guardian.config import settings
from guardian.devices import virtual
from guardian.models import CareSchedule, Home, Resident
from guardian.ring import poller
from guardian.state import routine

# (sandbox kind, name, fixed id, room, battery). Names match ring-sandbox scenario steps.
RING_DEVICES = [
    ("doorbell", "Front Door", "ava1.ring.device.GUARDIANDOORBELL001", "Front Door", 82),
    ("contact_sensor", "Front Door Sensor", "ava1.ring.device.GUARDIANCONTACT0001", "Front Door", 91),
    ("flood_freeze_sensor", "Kitchen Leak Sensor", "ava1.ring.device.GUARDIANFLOODKITCH", "Kitchen", 88),
    ("flood_freeze_sensor", "Bathroom Leak Sensor", "ava1.ring.device.GUARDIANFLOODBATHR", "Bathroom", 76),
    ("chime", "Kitchen Chime", "ava1.ring.device.GUARDIANCHIMEKITCH", "Kitchen", None),
]
DEVICE_IDS = {name: dev_id for _, name, dev_id, _, _ in RING_DEVICES}
poller.ROOMS.update({name: room for _, name, _, room, _ in RING_DEVICES})


def seed_db() -> None:
    db.clear_all()
    with db.session() as s:
        s.add(Home(name="Sharma Residence", timezone=settings.home_timezone, mode="home"))
        residents = [
            Resident(name="Priya", role="owner"),  # Kamla's daughter, lives in another city
            Resident(name="Rohan", role="family"),  # Priya's brother
            Resident(name="Kamla", role="resident_elder"),  # 74
            Resident(name="Sunita", role="caregiver"),
            Resident(name="Mr. Verma", role="neighbor"),
        ]
        s.add_all(residents)
        s.commit()
        sunita = s.exec(select(Resident).where(Resident.name == "Sunita")).one()
        s.add(
            CareSchedule(
                caregiver_id=sunita.id,
                weekdays=[0, 1, 2, 3, 4, 5],  # Mon-Sat
                start_local="10:00",
                end_local="12:00",
                min_duration_min=90,
            )
        )
        s.commit()
    virtual.forget_all()
    virtual.ensure_all()
    routine.routine_device()


def is_seeded() -> bool:
    with db.session() as s:
        return s.exec(select(Home)).first() is not None


async def seed_emulator(control: httpx.AsyncClient, register_webhook: bool = True) -> None:
    await control.delete("/_sandbox/webhooks")  # no device_removed noise while we rebuild
    (await control.post("/_sandbox/reset")).raise_for_status()
    state = (await control.get("/_sandbox/state")).json()
    for dev in state["devices"]:
        await control.delete(f"/_sandbox/devices/{dev['id']}")
    for kind, name, dev_id, _, battery in RING_DEVICES:
        body = {"kind": kind, "name": name, "id": dev_id}
        if battery is not None:
            body["battery"] = battery
        resp = await control.post("/_sandbox/devices", json=body)
        resp.raise_for_status()
    if register_webhook:
        url = settings.public_url.rstrip("/") + "/webhooks/ring"
        resp = await control.post("/_sandbox/webhooks", json={"url": url, "signing_key": settings.ring_webhook_secret})
        resp.raise_for_status()
