"""Playbooks as plain Python step lists. The runner knows how to execute each step kind.

Step params may include `only_if` (a named condition in runner.ONLY_IF). Message templates can
use {mode}, {name} (the resident elder), {last_activity}, {fire} and {degraded}.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from guardian.models import Device, Signal

STEP_KINDS = {"gather", "notify", "act", "wait_approval", "verify", "checkin", "escalate", "report"}
EXTERIOR_ROOMS = {"Front Door"}
SAFETY_CRITICAL_KINDS = {"flood_sensor", "contact_sensor", "doorbell", "smoke_alarm", "valve"}


@dataclass(frozen=True)
class Step:
    kind: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert self.kind in STEP_KINDS, self.kind


@dataclass(frozen=True)
class Playbook:
    id: str
    severity: str
    triggers: set[str]  # signal types that open (or corroborate) an incident
    related: set[str]  # other signal types attached as evidence while open
    steps: list[Step]
    title: Callable[[Device | None, Signal | None], str]
    # extra trigger condition given (signal, device, home snapshot)
    condition: Callable[[Signal, Device, dict], bool] = lambda sig, dev, home: True


WATER_LEAK = Playbook(
    id="water_leak",
    severity="critical",
    triggers={"water.detected"},
    related={"water.cleared", "valve.state"},
    title=lambda dev, sig=None: f"Water leak — {dev.room or dev.name}" if dev else "Water leak",
    steps=[
        Step("gather", "Gather home state, sensors, valve, health, snapshot",
             {"items": ["home_state", "flood_sensors", "valve", "device_health", "room_snapshot"]}),
        Step("gather", "Wait for corroboration from another flood sensor",
             {"items": ["corroboration"]}),
        Step("notify", "Alert owner", {"to": "owner", "message": "Water detected at home"}),
        Step("act", "Request valve close (policy)", {"action": "valve.close", "target_kind": "valve"}),
        Step("wait_approval", "Owner approval for valve close (if policy requires it)",
             {"action": "valve.close"}),
        Step("verify", "Verify valve closed and no new sensors wet", {"check": "valve_closed_and_dry"}),
        Step("escalate", "Escalate if water could not be confirmed off",
             {"message": "Could not confirm water is off"}),
        Step("report", "Write incident report"),
    ],
)


def _entry_condition(sig: Signal, dev: Device, home: dict) -> bool:
    from guardian.state import care_visits  # local import: care_visits imports the engine

    if home["mode"] not in ("away", "night"):
        return False
    if care_visits.visit_in_progress():
        return False
    if sig.type == "contact.opened":
        return dev.kind == "contact_sensor" and dev.room in EXTERIOR_ROOMS
    return sig.type == "motion.human" and dev.kind == "doorbell"


SUSPICIOUS_ENTRY = Playbook(
    id="suspicious_entry",
    severity="warning",
    triggers={"contact.opened", "motion.human"},
    related={"doorbell.pressed", "contact.closed"},
    title=lambda dev, sig=None: f"Possible entry — {dev.room or dev.name}" if dev else "Possible entry",
    condition=_entry_condition,
    steps=[
        Step("gather", "Gather snapshot, doorbell presses, other contact sensors",
             {"items": ["home_state", "trigger_snapshot", "doorbell_presses", "contact_sensors"]}),
        Step("notify", "Ask owner what to do",
             {"to": "owner", "message": "Possible entry while the home is in {mode} mode", "decision": True}),
        Step("act", "Offer a chime warning tone (owner approval)",
             {"action": "chime.play", "target_kind": "chime", "background": True, "optional": True}),
        Step("wait_approval", "Wait for owner decision", {"decision": True}),
        Step("report", "Write incident report"),
    ],
)

CAREGIVER_NO_SHOW = Playbook(
    id="caregiver_no_show",
    severity="warning",
    triggers=set(),  # opened by care_visits, not by a signal
    related=set(),
    title=lambda dev, sig=None: "Caregiver no-show",
    steps=[
        Step("gather", "Gather home state", {"items": ["home_state"]}),
        Step("notify", "Notify family", {"to": "family", "message": "Scheduled caregiver did not arrive"}),
        Step("escalate", "Hand over to family", {"always": True, "notify": False, "ladder": []}),
        Step("report", "Write incident report"),
    ],
)

MISSED_ROUTINE = Playbook(
    id="missed_routine",
    severity="warning",
    triggers={"routine.deviation"},
    related=set(),
    title=lambda dev, sig=None: "Unusual day — " + ((sig.value.get("rule") if sig else "") or "routine").replace("_", " "),
    condition=lambda sig, dev, home: sig.value.get("rule") != "low_activity_trend",  # trends: family digest only
    steps=[
        Step("gather", "Gather home state and last activity", {"items": ["home_state", "last_activity"]}),
        Step("checkin", "Check in with the resident (Voice Console)", {
            "kind": "wellbeing",
            "prompt_en": "Hello {name}, this is Guardian, just checking in. Are you okay?",
            "prompt_hi": "नमस्ते {name} जी, मैं Guardian हूँ, बस हाल-चाल पूछ रहा हूँ। क्या आप ठीक हैं?",
        }),
        Step("notify", "Alert family", {
            "to": "family", "only_if": "not_ok", "no_trigger_text": True,
            "message": "Please check on {name}. Last front-door activity: {last_activity}",
        }),
        Step("escalate", "Repeat until a family member acknowledges", {
            "only_if": "not_ok", "to": "family", "notify": False, "ladder": ["family"], "repeat": "routine",
            "message": "Please check on {name}. Last front-door activity: {last_activity}",
        }),
        Step("report", "Write incident report"),
    ],
)

TEMPERATURE_RISK = Playbook(
    id="temperature_risk",
    severity="critical",
    triggers={"temperature.freeze", "temperature.low", "temperature.high", "temperature.exceeded"},
    related={"thermostat.state", "temperature.normal"},
    title=lambda dev, sig=None: ("Freeze risk" if sig and sig.type in ("temperature.freeze", "temperature.low") else "Heat risk") + (f" — {dev.room}" if dev and dev.room else ""),
    steps=[
        Step("gather", "Gather home state and temperatures", {"items": ["home_state", "temperatures", "temp_kind"]}),
        Step("gather", "Wait for a second temperature source", {"items": ["corroboration"]}),
        Step("notify", "Alert owner", {"to": "owner", "message": "Temperature risk at home"}),
        Step("act", "Request heating to 16 °C (policy)", {
            "only_if": "freeze_and_empty", "action": "thermostat.set_heat", "target_kind": "thermostat", "args": {"setpoint_c": 16},
        }),
        Step("wait_approval", "Owner approval for heating (if policy requires it)", {"only_if": "freeze_and_empty", "action": "thermostat.set_heat"}),
        Step("verify", "Verify the temperature is rising", {"only_if": "freeze_and_empty", "check": "temperature_rising"}),
        Step("checkin", "Check in with the resident about the heat", {
            "only_if": "heat_and_occupied", "kind": "wellbeing", "close_on_ok": False,
            "prompt_en": "{name}, it is very hot at home. Please drink some water and stay cool. Are you okay?",
            "prompt_hi": "{name} जी, घर में बहुत गर्मी है। कृपया पानी पिएँ। क्या आप ठीक हैं?",
        }),
        Step("notify", "Tell family about the heat", {"only_if": "heat_and_occupied", "to": "family", "message": "Very hot at home with {name} inside"}),
        Step("act", "Offer to switch off the heater plug (owner approval)", {
            "only_if": "heat", "action": "plug.off", "target_kind": "plug", "optional": True,
        }),
        Step("wait_approval", "Owner approval for switching off the heater", {"only_if": "heat", "action": "plug.off"}),
        Step("escalate", "Escalate if the temperature risk wasn't handled", {"message": "Temperature risk not resolved"}),
        Step("report", "Write incident report"),
    ],
)

SMOKE_CO = Playbook(
    id="smoke_co",
    severity="critical",
    triggers={"smoke.detected", "co.detected"},
    related={"smoke.cleared", "thermostat.state"},
    title=lambda dev, sig=None: ("Carbon monoxide alarm" if sig and sig.type == "co.detected" else "Smoke alarm") + " (advisory)",
    steps=[
        Step("gather", "Gather home state", {"items": ["home_state", "emergency_disclaimer"]}),
        Step("notify", "Alert everyone", {
            "to": "everyone", "no_trigger_text": True,
            "message": "SMOKE/CO ALARM at home. Everyone should leave now and call fire services on {fire}. "
            "Guardian has not called emergency services.",
        }),
        Step("checkin", "Tell occupants to leave (Voice Console)", {
            "kind": "announcement",
            "prompt_en": "Smoke alarm! Please leave the house now. Call fire services on {fire}.",
            "prompt_hi": "धुएँ का अलार्म! कृपया अभी घर से बाहर निकलें। फायर सर्विस को {fire} पर कॉल करें।",
        }),
        Step("act", "Switch off the HVAC fan (policy)", {"action": "thermostat.fan_off", "target_kind": "thermostat", "optional": True}),
        Step("escalate", "Keep alerting until someone acknowledges", {"always": True, "notify": False, "message": "Smoke/CO alarm at home — please acknowledge"}),
        Step("report", "Write incident report"),
    ],
)


def _health_condition(sig: Signal, dev: Device, home: dict) -> bool:
    return dev.kind in SAFETY_CRITICAL_KINDS


DEVICE_HEALTH = Playbook(
    id="device_health",
    severity="warning",
    triggers={"device.offline", "device.tamper", "device.stale"},
    related={"device.online", "device.tamper_cleared"},
    title=lambda dev, sig=None: f"Protection degraded — {dev.name}" if dev else "Protection degraded",
    condition=_health_condition,
    steps=[
        Step("gather", "Work out which protection is degraded", {"items": ["degraded_dependents"]}),
        Step("notify", "Tell the owner", {"to": "owner", "message": "Device problem ({degraded} degraded)"}),
        Step("escalate", "Hand over to the owner", {"always": True, "notify": False, "ladder": []}),
        Step("report", "Write incident report"),
    ],
)

PLAYBOOKS: dict[str, Playbook] = {
    p.id: p for p in (WATER_LEAK, SUSPICIOUS_ENTRY, CAREGIVER_NO_SHOW, MISSED_ROUTINE, TEMPERATURE_RISK, SMOKE_CO, DEVICE_HEALTH)
}
