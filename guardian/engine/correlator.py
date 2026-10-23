"""Signal -> Incident. Runs as one consumer task so incidents are never double-opened."""

import logging
from datetime import timedelta

from guardian import clock, db
from guardian.bus import bus
from guardian.engine import incidents, runner
from guardian.engine.playbooks import PLAYBOOKS
from guardian.models import Device, Signal
from guardian.state import home_state

log = logging.getLogger("guardian.correlator")
REOPEN_WINDOW = timedelta(minutes=10)


def describe(sig: Signal, dev: Device) -> str:
    label = {
        "water.detected": "turned wet",
        "water.cleared": "dried",
        "contact.opened": "opened",
        "contact.closed": "closed",
        "motion.human": "detected a person",
        "motion.other": "detected motion",
        "doorbell.pressed": "doorbell pressed",
        "device.online": "came online",
        "device.offline": "went offline",
        "valve.state": f"is {sig.value.get('position')}",
        "thermostat.state": f"reads {sig.value.get('current_c')} °C, mode {sig.value.get('mode')}",
        "plug.state": "is " + ("on" if sig.value.get("on") else "off"),
        "temperature.freeze": "reported freezing temperature",
        "temperature.freeze_cleared": "freeze cleared",
        "temperature.low": f"reads {sig.value.get('current_c')} °C (below {sig.value.get('limit_c')} °C)",
        "temperature.high": f"reads {sig.value.get('current_c')} °C (above {sig.value.get('limit_c')} °C)",
        "temperature.normal": f"back to {sig.value.get('current_c')} °C",
        "temperature.exceeded": "reported temperature outside its limits",
        "smoke.detected": "detected SMOKE",
        "co.detected": "detected CARBON MONOXIDE",
        "smoke.cleared": "alarm cleared",
        "device.tamper": "reported tampering",
        "device.tamper_cleared": "tamper cleared",
        "device.stale": "has stopped reporting",
        "routine.deviation": sig.value.get("reason", "routine deviation"),
    }.get(sig.type, sig.type)
    return label if sig.type == "routine.deviation" else f"{dev.name} {label}"


def attach_signal(incident_id: str, sig: Signal, dev: Device, corroborates: bool) -> None:
    incidents.add_evidence(
        incident_id,
        "signal",
        describe(sig, dev),
        {
            "signal_id": sig.id,
            "device_id": dev.id,
            "type": sig.type,
            "observed_at": clock.as_utc(sig.observed_at).isoformat(),
            "corroborates": corroborates,
            **({"reason": sig.value["reason"], "rule": sig.value.get("rule")} if sig.type == "routine.deviation" else {}),
        },
    )
    if corroborates:
        incidents.recompute_confidence(incident_id)


def handle_signal(signal_id: str) -> None:
    with db.session() as s:
        sig = s.get(Signal, signal_id)
        dev = s.get(Device, sig.device_id) if sig else None
    if sig is None or dev is None:
        return
    home = home_state.snapshot()
    for pb in PLAYBOOKS.values():
        open_ = incidents.open_incidents(pb.id)
        if sig.type in pb.triggers:
            recent = [i for i in open_ if clock.now() - clock.as_utc(i.opened_at) <= REOPEN_WINDOW]
            if recent:
                attach_signal(recent[0].id, sig, dev, corroborates=True)
            elif pb.condition(sig, dev, home):
                inc = incidents.open_incident(pb.id, pb.title(dev, sig))
                attach_signal(inc.id, sig, dev, corroborates=True)
                runner.start(inc.id)
        elif sig.type in pb.related:
            for inc in open_:
                attach_signal(inc.id, sig, dev, corroborates=False)


async def run_forever() -> None:
    q = bus.subscribe({"signal"})
    try:
        while True:
            _, payload = await q.get()
            try:
                handle_signal(payload["id"])
            except Exception:  # keep consuming; one bad signal must not stop the engine
                log.exception("correlator failed on signal %s", payload.get("id"))
    finally:
        bus.unsubscribe(q)
