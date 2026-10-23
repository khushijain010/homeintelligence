"""Runs playbook steps: one asyncio task per incident. Writes Evidence for everything it does.

Progress (step index, variables, the current wait's deadline) is saved after every step so a
restarted Guardian resumes open incidents. Every action carries an idempotency key, so a step
that runs again after a restart finds its earlier action instead of acting twice.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import select

from guardian import clock, db
from guardian.ai import report_llm
from guardian.bus import bus
from guardian.config import settings
from guardian.devices import actuators, registry, virtual
from guardian.engine import escalation, incidents, policy, report, verifier
from guardian.engine.playbooks import PLAYBOOKS, Playbook, Step
from guardian.models import Action, Device, Incident, Resident, Signal
from guardian.state import home_state

log = logging.getLogger("guardian.runner")
POLL = 0.2

DECISIONS = {
    "known_person": ("false_alarm", "Known person"),
    "handle_it": ("resolved", "I'll handle it"),
    "escalate": ("escalated", "Escalate"),
}


@dataclass
class Run:
    incident_id: str
    playbook: Playbook
    vars: dict[str, Any] = field(default_factory=dict)
    failed: str | None = None  # why we must escalate
    step_index: int = 0
    counter: int = 0  # actions requested within the current step (idempotency keys)
    resume_deadline: datetime | None = None  # deadline of a wait interrupted by a restart

    @property
    def trigger_device(self) -> Device | None:
        for ev in incidents.evidence(self.incident_id):
            if ev.kind == "signal" and ev.payload.get("corroborates"):
                return registry.get(ev.payload["device_id"])
        return None


def save_vars(incident_id: str, **updates: Any) -> None:
    inc = incidents.get(incident_id)
    if inc is not None:
        incidents.update(incident_id, run_vars={**(inc.run_vars or {}), **updates})


def _save_progress(run: Run, step_index: int) -> None:
    incidents.update(
        run.incident_id, step_index=step_index, run_vars={**run.vars, "failed": run.failed}, step_deadline=None
    )


def step_timer(run: Run, seconds: float) -> clock.Timer:
    """A wait whose deadline survives a restart."""
    if run.resume_deadline is not None:
        deadline, run.resume_deadline = run.resume_deadline, None
        return clock.Timer(seconds, start=clock.as_utc(deadline) - timedelta(seconds=seconds))
    t = clock.Timer(seconds)
    incidents.update(run.incident_id, step_deadline=t.deadline)
    return t


# --------------------------------------------------------------------------- actions


def _target(device: Device | None) -> policy.Target | None:
    if device is None:
        return None
    if device.source == "virtual":
        dev = virtual.get_virtual(device.id)
        if dev:
            dev.read_state()  # a read refreshes the virtual device's health report
        device = registry.get(device.id)
    usable, problems = registry.reliability(device)
    return policy.Target(
        name=device.name,
        online=device.online,
        fault=device.state.get("position") == "fault" or bool(device.state.get("fault")),
        fresh=not any("not reported" in p or "never reported" in p for p in problems),
    )


def _context(run: Run, action_type: str, device: Device | None, owner_approved: bool, exclude: str | None, params: dict | None = None):
    inc = incidents.get(run.incident_id)
    prior = [
        a
        for a in incidents.actions(run.incident_id)
        if a.action_type == action_type
        and a.target_device_id == (device.id if device else None)
        and a.result is not None
        and a.id != exclude
    ]
    return policy.PolicyContext(
        action_type=action_type,
        target=_target(device),
        confidence=inc.confidence,
        signal_devices=len(incidents.signal_devices(run.incident_id)),
        occupancy=home_state.snapshot()["occupancy"],
        water_incident_open=bool(incidents.open_incidents("water_leak")),
        smoke_incident_open=bool(incidents.open_incidents("smoke_co")),
        prior_same_action=len(prior),
        owner_approved=owner_approved,
        params=params or {},
    )


def evaluate(run: Run, action_type: str, device: Device | None, owner_approved: bool = False, exclude: str | None = None, params: dict | None = None) -> policy.Decision:
    return policy.evaluate(_context(run, action_type, device, owner_approved, exclude, params), policy.current_rules())


def _record_decision(run: Run, action_type: str, decision: policy.Decision, approved: bool = False) -> None:
    label = " (re-check after owner approval)" if approved else ""
    incidents.add_evidence(
        run.incident_id,
        "policy_decision",
        f"Policy: {action_type} → {decision.outcome}{label}",
        {"action_type": action_type, "outcome": decision.outcome, "reasons": decision.reasons},
    )


async def _execute(run: Run, action: Action, decision: policy.Decision, device: Device | None, params: dict) -> None:
    outcome = await actuators.execute(decision, action.action_type, device, params)
    action.result = outcome.result
    if outcome.data.get("sns_message_id"):
        incidents.add_evidence(
            run.incident_id,
            "verification",
            f"Alert sent by SMS/email via Amazon SNS to {params.get('role', 'owner')}",
            {"sns_message_id": outcome.data["sns_message_id"], "role": params.get("role")},
        )
    if outcome.verified is not None:
        action.verified = outcome.verified
    elif action.action_type == "snapshot.capture" and outcome.result == "ok":
        incidents.add_evidence(run.incident_id, "snapshot", outcome.detail, outcome.data)
        action.verified = "yes" if outcome.data.get("data_uri") else "no"
    elif action.action_type.startswith("notify.") or action.action_type == "chime.play":
        action.verified = "yes" if outcome.result == "ok" else "no"  # readback: delivered/accepted
    elif outcome.result != "ok":
        action.verified = "no"
    incidents.save_action(action)


def _by_key(key: str) -> Action | None:
    with db.session() as s:
        return s.exec(select(Action).where(Action.idempotency_key == key)).first()


async def request_action(
    run: Run, action_type: str, device: Device | None = None, params: dict | None = None, key: str | None = None
) -> Action:
    """The only path from a playbook to an actuator: policy decides, then execute."""
    run.counter += 1
    key = key or f"{run.incident_id}:{run.step_index}:{action_type}:{run.counter}"
    existing = _by_key(key)
    if existing is not None:
        if existing.policy_outcome == "allow" and existing.result is None:  # interrupted mid-execution
            decision = evaluate(run, action_type, device, exclude=existing.id, params=params)
            if decision.outcome == "allow":
                await _execute(run, existing, decision, device, params or {})
        return existing
    decision = evaluate(run, action_type, device, params=params)
    _record_decision(run, action_type, decision)
    action = Action(
        incident_id=run.incident_id,
        action_type=action_type,
        target_device_id=device.id if device else None,
        policy_outcome=decision.outcome,
        policy_reasons=decision.reasons,
        approval_status="pending" if decision.outcome == "needs_approval" else "n/a",
        idempotency_key=key,
    )
    incidents.save_action(action)
    if decision.outcome == "allow":
        await _execute(run, action, decision, device, params or {})
    elif decision.outcome == "needs_approval":
        from guardian.notify import sns  # alert the owner's phone that a card is waiting

        sns.approval_needed(run.incident_id, action)
    return action


def _fmt(run: Run, text: str) -> str:
    with db.session() as s:
        elder = s.exec(select(Resident).where(Resident.role == "resident_elder")).first()
    home = run.vars.get("home") or {}
    return text.format(
        mode=home.get("mode", home_state.get_home().mode),
        name=elder.name if elder else "the resident",
        last_activity=run.vars.get("last_activity", "unknown"),
        fire=settings.emergency_number_fire,
        degraded=run.vars.get("degraded", "a safety device"),
    )


def _recipients(to: str) -> list[str]:
    roles = {"owner": ["owner"], "family": ["owner", "family"], "neighbor": ["neighbor"], "everyone": ["owner", "family", "neighbor", "resident_elder", "caregiver"]}[to]
    with db.session() as s:
        return [r.name for r in s.exec(select(Resident).where(Resident.role.in_(roles))).all()]  # type: ignore[attr-defined]


async def notify_role(incident_id: str, role: str, message: str, key: str | None = None, critical: bool = False) -> Action:
    """Policy-gated notification outside the normal step flow (ladder, re-alerts)."""
    inc = incidents.get(incident_id)
    run = Run(incident_id, PLAYBOOKS[inc.playbook_id], step_index=inc.step_index or 0)
    params = {"to": ", ".join(_recipients(role)) or role, "role": role, "message": message, "incident_id": incident_id, "decision": False, "critical": critical}
    return await request_action(run, f"notify.{role}", None, params, key=key)


# --------------------------------------------------------------------------- gatherers


def _g_home_state(run: Run) -> None:
    snap = home_state.snapshot()
    run.vars["home"] = snap
    incidents.add_evidence(
        run.incident_id,
        "home_state",
        f"Home is {snap['occupancy']} ({snap['mode']} mode): {'; '.join(snap['reasons'])}",
        snap,
    )


def _g_flood_sensors(run: Run) -> None:
    sensors = registry.all_devices("flood_sensor")
    parts = [f"{d.name} {'wet' if d.state.get('wet') else 'dry'}" for d in sensors]
    incidents.add_evidence(
        run.incident_id,
        "device_health",
        "Flood sensors: " + (", ".join(parts) or "none"),
        {"sensors": {d.id: bool(d.state.get("wet")) for d in sensors}},
    )


def _g_valve(run: Run) -> None:
    for d in registry.all_devices("valve"):
        valve = virtual.get_valve(d.id)
        position = valve.read_state() if valve else "unknown"
        incidents.add_evidence(
            run.incident_id,
            "device_health",
            f"{d.name} (Simulated) reads {position}",
            {"device_id": d.id, "position": position},
        )


def _g_device_health(run: Run) -> None:
    for d in registry.all_devices("flood_sensor") + registry.all_devices("valve"):
        usable, problems = registry.reliability(d)
        incidents.add_evidence(
            run.incident_id,
            "device_health",
            f"{d.name}: {'usable' if usable else 'unusable — ' + '; '.join(problems)}",
            {"device_id": d.id, "usable": usable, "problems": problems},
        )


def _cameras() -> list[Device]:
    return registry.all_devices("doorbell") + registry.all_devices("camera")


async def _g_room_snapshot(run: Run) -> None:
    trig = run.trigger_device
    room = trig.room if trig else ""
    cams = [c for c in _cameras() if room and c.room == room]
    if not cams:
        incidents.add_evidence(
            run.incident_id, "snapshot", f"No camera covers {room or 'this room'}; no snapshot taken", {}
        )
        return
    await request_action(run, "snapshot.capture", cams[0])


async def _g_trigger_snapshot(run: Run) -> None:
    trig = run.trigger_device
    cams = [c for c in _cameras() if trig and c.room == trig.room] or registry.all_devices("doorbell")
    if not cams:
        incidents.add_evidence(run.incident_id, "snapshot", "No camera available; no snapshot taken", {})
        return
    await request_action(run, "snapshot.capture", cams[0])


def _g_doorbell_presses(run: Run) -> None:
    """A press shortly before the entry suggests a visitor: confidence −0.2."""
    first = next(
        (e for e in incidents.evidence(run.incident_id) if e.kind == "signal" and e.payload.get("corroborates")),
        None,
    )
    if first is None:
        return
    entry_at = clock.as_utc(datetime.fromisoformat(first.payload["observed_at"]))
    with db.session() as s:
        presses = s.exec(
            select(Signal, Device)
            .where(Signal.device_id == Device.id)
            .where(Signal.type == "doorbell.pressed")
            .where(Signal.observed_at >= entry_at - timedelta(minutes=5))
            .where(Signal.observed_at <= entry_at)
        ).all()
    if not presses:
        incidents.add_evidence(
            run.incident_id, "device_health", "No doorbell press in the 5 min before entry", {"presses": 0}
        )
        return
    sig, dev = presses[-1]
    secs = int((entry_at - clock.as_utc(sig.observed_at)).total_seconds())
    incidents.add_evidence(
        run.incident_id,
        "signal",
        f"{dev.name} doorbell pressed {secs} s before entry (likely a visitor)",
        {"signal_id": sig.id, "device_id": dev.id, "type": sig.type, "corroborates": False, "confidence_delta": -0.2},
    )
    incidents.recompute_confidence(run.incident_id)


def _g_contact_sensors(run: Run) -> None:
    trig = run.trigger_device
    others = [d for d in registry.all_devices("contact_sensor") if not trig or d.id != trig.id]
    parts = [f"{d.name} {'open' if d.state.get('open') else 'closed'}" for d in others]
    incidents.add_evidence(
        run.incident_id,
        "device_health",
        "Other contact sensors: " + (", ".join(parts) or "none installed"),
        {"sensors": {d.id: bool(d.state.get("open")) for d in others}},
    )


async def _g_corroboration(run: Run) -> None:
    timer = step_timer(run, settings.corroboration_seconds)
    while len(incidents.signal_devices(run.incident_id)) < 2 and not timer.expired():
        await asyncio.sleep(POLL)
    n = len(incidents.signal_devices(run.incident_id))
    if n < 2:
        incidents.add_evidence(
            run.incident_id,
            "device_health",
            f"No second sensor confirmed within {settings.corroboration_seconds:g} s",
            {"signal_devices": n},
        )


def _g_last_activity(run: Run) -> None:
    from guardian.state import routine

    text = routine.describe_last_activity()
    run.vars["last_activity"] = text
    sig = next((e for e in incidents.evidence(run.incident_id) if e.kind == "signal"), None)
    reason = (sig.payload.get("reason") if sig else None) or ""
    incidents.add_evidence(run.incident_id, "home_state", f"Last front-door activity: {text}", {"last_activity": text, "reason": reason})


def _g_temperatures(run: Run) -> None:
    for d in registry.all_devices("thermostat"):
        dev = virtual.get_virtual(d.id)
        if dev:
            r = dev.read_state()
            incidents.add_evidence(
                run.incident_id,
                "device_health",
                f"{d.name} (Simulated): {r['current_c']:.1f} °C, mode {r['mode']}, setpoint {r['setpoint_c']:g} °C",
                {"device_id": d.id, **r},
            )


def _g_temp_kind(run: Run) -> None:
    types = {e.payload.get("type") for e in incidents.evidence(run.incident_id) if e.kind == "signal" and e.payload.get("corroborates")}
    kind = "freeze" if types & {"temperature.freeze", "temperature.low"} else "heat"
    run.vars["temp_kind"] = kind
    label = "Freeze risk (below %g °C or freeze sensor)" % settings.temp_low_c if kind == "freeze" else "Heat risk (above %g °C)" % settings.temp_high_c
    incidents.add_evidence(run.incident_id, "home_state", label, {"temp_kind": kind})


def _g_emergency_disclaimer(run: Run) -> None:
    incidents.add_evidence(
        run.incident_id,
        "verification",
        f"Guardian did not contact emergency services; occupants were shown the fire services number {settings.emergency_number_fire}",
        {"emergency_services_contacted": False, "number_shown": settings.emergency_number_fire},
    )


def _g_degraded_dependents(run: Run) -> None:
    """Which protection is degraded, and lower confidence of open incidents relying on this device."""
    trig = run.trigger_device
    if trig is None:
        return
    protects = {
        "flood_sensor": f"leak detection in {trig.room or trig.name}",
        "contact_sensor": f"entry detection at {trig.room or trig.name}",
        "doorbell": "front-door camera evidence and caregiver visit checks",
        "camera": "camera evidence",
        "smoke_alarm": "smoke and CO detection",
        "valve": "automatic water shut-off",
    }.get(trig.kind, trig.name)
    run.vars["degraded"] = protects
    incidents.add_evidence(run.incident_id, "device_health", f"Degraded protection: {protects}", {"device_id": trig.id, "protects": protects})
    for other in incidents.open_incidents():
        if other.id != run.incident_id and trig.id in incidents.signal_devices(other.id):
            before = other.confidence
            incidents.add_evidence(
                other.id, "device_health", f"{trig.name} became unreliable; evidence from it now counts less", {"device_id": trig.id}
            )
            after = incidents.recompute_confidence(other.id)
            incidents.add_evidence(
                run.incident_id,
                "device_health",
                f"Lowered confidence of '{other.title}' from {before:.2f} to {after:.2f}",
                {"incident_id": other.id, "before": before, "after": after},
            )


GATHERERS = {
    "temp_kind": _g_temp_kind,
    "emergency_disclaimer": _g_emergency_disclaimer,
    "degraded_dependents": _g_degraded_dependents,
    "home_state": _g_home_state,
    "flood_sensors": _g_flood_sensors,
    "valve": _g_valve,
    "device_health": _g_device_health,
    "room_snapshot": _g_room_snapshot,
    "trigger_snapshot": _g_trigger_snapshot,
    "doorbell_presses": _g_doorbell_presses,
    "contact_sensors": _g_contact_sensors,
    "corroboration": _g_corroboration,
    "last_activity": _g_last_activity,
    "temperatures": _g_temperatures,
}


# --------------------------------------------------------------------------- step kinds


async def _gather(run: Run, step: Step) -> None:
    for item in step.params["items"]:
        result = GATHERERS[item](run)
        if asyncio.iscoroutine(result):
            await result


async def _notify(run: Run, step: Step) -> None:
    to = step.params["to"]
    home = run.vars.get("home") or home_state.snapshot()
    evidence = incidents.evidence(run.incident_id)
    first = next((e for e in evidence if e.kind == "signal"), None)
    message = _fmt(run, step.params["message"])
    if first and not step.params.get("no_trigger_text"):
        message += f": {first.payload.get('reason') or first.summary}"
    if run.playbook.id == "water_leak":
        message += f". Home is {home['occupancy']}."
    params = {
        "to": ", ".join(_recipients(to)) or to,
        "role": to,
        "message": message,
        "incident_id": run.incident_id,
        "decision": bool(step.params.get("decision")),
        "critical": run.playbook.severity == "critical",
    }
    await request_action(run, f"notify.{to}", None, params)
    if step.params.get("decision"):
        incidents.update(run.incident_id, status="awaiting_approval")


async def _act(run: Run, step: Step) -> None:
    devices = [d for d in registry.all_devices(step.params["target_kind"]) if step.params.get("target_name") in (None, d.name)]
    if not devices:
        if step.params.get("optional"):
            return
        run.failed = f"no {step.params['target_kind']} installed"
        return
    run.vars["wet_at_action"] = sorted(verifier.wet_sensors())
    if devices[0].kind == "thermostat":
        run.vars["temp_at_action"] = virtual.get_virtual(devices[0].id).read_state()["current_c"]
    action = await request_action(run, step.params["action"], devices[0], dict(step.params.get("args", {})))
    if step.params.get("background"):
        run.vars.setdefault("side_actions", []).append(action.id)  # approved later, while waiting
        return
    run.vars["action_id"] = action.id
    if action.policy_outcome == "allow":
        incidents.update(run.incident_id, status="acting")
    elif action.policy_outcome == "needs_approval":
        incidents.update(run.incident_id, status="awaiting_approval")
    else:
        run.failed = f"policy denied {action.action_type}: {action.policy_reasons[-1]}"


def _load_action(action_id: str) -> Action:
    with db.session() as s:
        return s.get(Action, action_id)


async def _execute_approved(run: Run, action: Action) -> bool:
    """Owner approved: re-run the policy right before executing. Returns True if it ran."""
    incidents.add_evidence(
        run.incident_id,
        "human_decision",
        f"Owner approved {action.action_type}" + (f" ({action.decided_by})" if action.decided_by else ""),
        {"action_id": action.id, "approval": "approved", "by": action.decided_by},
    )
    device = registry.get(action.target_device_id) if action.target_device_id else None
    params = dict(_step_args(run, action.action_type))
    decision = evaluate(run, action.action_type, device, owner_approved=True, exclude=action.id, params=params)
    _record_decision(run, action.action_type, decision, approved=True)
    if decision.outcome != "allow":
        return False
    await _execute(run, action, decision, device, params)
    return True


def _step_args(run: Run, action_type: str) -> dict:
    for step in run.playbook.steps:
        if step.kind == "act" and step.params.get("action") == action_type:
            return step.params.get("args", {})
    return {}


async def _process_side_approvals(run: Run) -> None:
    """Background actions (e.g. the chime) can be approved while the incident waits on something else."""
    for action_id in list(run.vars.get("side_actions", [])):
        action = _load_action(action_id)
        if action.approval_status == "approved" and action.result is None:
            await _execute_approved(run, action)
        if action.approval_status != "pending":
            run.vars["side_actions"].remove(action_id)


async def _wait_decision(run: Run) -> None:
    incidents.update(run.incident_id, status="awaiting_approval")
    while True:
        await _process_side_approvals(run)
        choice = next(
            (e for e in incidents.evidence(run.incident_id) if e.kind == "human_decision" and "choice" in e.payload),
            None,
        )
        if choice:
            incidents.update(run.incident_id, status=DECISIONS[choice.payload["choice"]][0])
            return
        await asyncio.sleep(POLL)


async def _wait_approval(run: Run, step: Step) -> None:
    if step.params.get("decision"):
        await _wait_decision(run)
        return
    action_id = run.vars.get("action_id")
    action = _load_action(action_id) if action_id else None
    if action is None or action.approval_status != "pending":
        if action is not None and action.approval_status == "approved" and action.result is None:
            if not await _execute_approved(run, action):  # approved just before a restart
                run.failed = "policy re-check refused the approved action"
        return
    incidents.add_evidence(
        run.incident_id,
        "policy_decision",
        f"Waiting up to {settings.approval_timeout_seconds:g} s for owner approval of {action.action_type}",
        {"action_id": action.id},
    )
    timer = step_timer(run, settings.approval_timeout_seconds)
    while action.approval_status == "pending" and not timer.expired():
        await asyncio.sleep(POLL)
        action = _load_action(action.id)
    if action.approval_status == "pending":
        action.approval_status = "expired"
        incidents.save_action(action)
    if action.approval_status == "expired":
        incidents.add_evidence(
            run.incident_id,
            "policy_decision",
            f"No owner answer within {settings.approval_timeout_seconds:g} s; approval for {action.action_type} expired",
            {"action_id": action.id, "approval": "expired"},
        )
    if action.approval_status != "approved":
        if action.approval_status == "rejected":
            incidents.add_evidence(
                run.incident_id,
                "human_decision",
                f"Owner rejected {action.action_type}" + (f" ({action.decided_by})" if action.decided_by else ""),
                {"action_id": action.id, "approval": "rejected", "by": action.decided_by},
            )
        run.failed = f"owner approval {action.approval_status}"
        return
    run.vars["wet_at_action"] = sorted(verifier.wet_sensors())
    incidents.update(run.incident_id, status="acting")
    if not await _execute_approved(run, action):
        last = [e for e in incidents.evidence(run.incident_id) if e.kind == "policy_decision"][-1]
        run.failed = f"policy re-check {last.payload.get('outcome')}: {(last.payload.get('reasons') or ['?'])[-1]}"


async def _verify(run: Run, step: Step) -> None:
    action_id = run.vars.get("action_id")
    action = _load_action(action_id) if action_id else None
    if action is None or action.result is None:
        return  # nothing ran; run.failed explains why
    incidents.update(run.incident_id, status="verifying")
    if action.result != "ok":
        action.verified = "no"
        incidents.save_action(action)
        run.failed = f"{action.action_type} {action.result}"
        return
    check = step.params.get("check", "valve_closed_and_dry")
    if check == "valve_closed_and_dry":
        verified, result = await verifier.valve_closed_and_dry(action, set(run.vars.get("wet_at_action", [])))
    else:
        verified, result = await verifier.temperature_rising(action, run.vars.get("temp_at_action"))
    action.verified, action.result = verified, result
    incidents.save_action(action)
    if verified == "yes":
        incidents.update(run.incident_id, status="contained")
    else:
        run.failed = "verification failed"


async def _checkin(run: Run, step: Step) -> None:
    """Ask the resident (simulated Alexa+ Voice Console) and wait for an answer."""
    from guardian.state import checkins

    with db.session() as s:
        elder = s.exec(select(Resident).where(Resident.role == "resident_elder")).first()
    name = elder.name if elder else "there"
    kind = step.params.get("kind", "wellbeing")
    c_id = run.vars.get("checkin_id")
    c = checkins.get(c_id) if c_id else None
    if c is None:
        c = checkins.create(kind, _fmt(run, step.params["prompt_en"]), _fmt(run, step.params["prompt_hi"]), incident_id=run.incident_id)
        run.vars["checkin_id"] = c.id
        save_vars(run.incident_id, checkin_id=c.id)
    if kind == "announcement":
        incidents.add_evidence(run.incident_id, "human_decision", f"Announced to occupants: {c.prompt_en}", {"checkin_id": c.id, "announcement": True})
        return
    incidents.update(run.incident_id, status="awaiting_approval")
    timer = step_timer(run, settings.checkin_timeout_seconds)
    answer = await checkins.wait_for_answer(c.id, max(timer.remaining(), 0))
    run.vars["checkin_answer"] = answer
    said = {
        "ok": f"{name} answered the check-in: she is okay",
        "needs_help": f"{name} answered the check-in: she needs help",
        "no_response": f"No answer from {name} within {settings.checkin_timeout_seconds / 60:g} min",
    }[answer]
    incidents.add_evidence(run.incident_id, "human_decision", said, {"checkin_id": c.id, "answer": answer, "transcript": checkins.get(c.id).transcript})
    if answer == "ok" and step.params.get("close_on_ok", True):
        incidents.update(run.incident_id, status="false_alarm")
        run.vars["skip_to_report"] = True
    elif answer != "ok":
        run.failed = f"resident {answer.replace('_', ' ')}"


async def _escalate(run: Run, step: Step) -> None:
    if not (run.failed or step.params.get("always")):
        return
    to = step.params.get("to", "owner")
    message = _fmt(run, step.params.get("message", "Guardian needs you"))
    if run.failed and not step.params.get("hide_reason"):
        message += f" ({run.failed})"
    if step.params.get("notify", True):
        await request_action(
            run,
            f"notify.{to}",
            None,
            {"to": ", ".join(_recipients(to)) or to, "role": to, "message": message, "incident_id": run.incident_id, "decision": False, "critical": True},
        )
    incidents.update(run.incident_id, status="escalated")
    roles = step.params.get("ladder")
    if roles is None:
        roles = [r.strip() for r in settings.escalation_roles.split(",") if r.strip()]
    if roles:
        interval = settings.routine_repeat_seconds if step.params.get("repeat") == "routine" else None
        run.vars["ladder"] = {"roles": roles, "message": message, "interval": interval}
        save_vars(run.incident_id, ladder=run.vars["ladder"], ladder_step=1)
        escalation.start(run.incident_id, roles, message, interval)


async def _report(run: Run, step: Step) -> None:
    inc = incidents.get(run.incident_id)
    if inc.status not in incidents.TERMINAL:
        incidents.update(run.incident_id, status="escalated" if run.failed else "resolved")
    incidents.update(run.incident_id, report_text=report.build(run.incident_id))
    # Template is always saved first; a grounded LLM report is shown on top of it when valid.
    if await report_llm.generate(run.incident_id):
        bus.publish("incident", {"id": run.incident_id})


STEP_HANDLERS = {
    "gather": _gather,
    "notify": _notify,
    "act": _act,
    "wait_approval": _wait_approval,
    "verify": _verify,
    "checkin": _checkin,
    "escalate": _escalate,
    "report": _report,
}

ONLY_IF = {
    "not_ok": lambda run: run.vars.get("checkin_answer") != "ok",
    "freeze_and_empty": lambda run: run.vars.get("temp_kind") == "freeze" and (run.vars.get("home") or {}).get("occupancy") == "empty",
    "freeze_and_occupied": lambda run: run.vars.get("temp_kind") == "freeze" and (run.vars.get("home") or {}).get("occupancy") != "empty",
    "heat": lambda run: run.vars.get("temp_kind") == "heat",
    "heat_and_occupied": lambda run: run.vars.get("temp_kind") == "heat" and (run.vars.get("home") or {}).get("occupancy") != "empty",
}


# --------------------------------------------------------------------------- task management

_tasks: dict[str, asyncio.Task] = {}


async def run_incident(incident_id: str) -> None:
    inc = incidents.get(incident_id)
    vars_ = dict(inc.run_vars or {})
    run = Run(incident_id, PLAYBOOKS[inc.playbook_id], vars=vars_, failed=vars_.pop("failed", None))
    start_at = inc.step_index or 0
    if start_at and inc.step_deadline:
        run.resume_deadline = clock.as_utc(inc.step_deadline)
    if start_at:
        incidents.add_evidence(incident_id, "verification", f"Guardian resumed this incident at step {start_at + 1} after a restart", {"step_index": start_at})
    try:
        for index in range(start_at, len(run.playbook.steps)):
            step = run.playbook.steps[index]
            run.step_index, run.counter = index, 0
            skip = (run.vars.get("skip_to_report") and step.kind != "report") or (
                step.params.get("only_if") and not ONLY_IF[step.params["only_if"]](run)
            )
            if not skip:
                incidents.update(incident_id, current_step=step.name)
                await STEP_HANDLERS[step.kind](run, step)
            _save_progress(run, index + 1)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # anything unexpected: escalate honestly
        log.exception("runner failed for incident %s", incident_id)
        incidents.add_evidence(incident_id, "verification", f"Guardian error, escalating: {exc}", {})
        incidents.update(incident_id, status="escalated", report_text=report.build(incident_id))
    finally:
        incidents.update(incident_id, current_step="done")


def start(incident_id: str) -> asyncio.Task:
    task = asyncio.create_task(run_incident(incident_id))
    _tasks[incident_id] = task
    task.add_done_callback(lambda _: _tasks.pop(incident_id, None))
    return task


def resume_all() -> list[str]:
    """On startup: continue open incidents from their saved step, and unfinished ladders."""
    resumed = []
    for inc in incidents.open_incidents():
        if (inc.step_index or 0) < len(PLAYBOOKS[inc.playbook_id].steps):
            start(inc.id)
            resumed.append(inc.id)
    with db.session() as s:
        escalated = s.exec(select(Incident).where(Incident.status == "escalated")).all()
    for inc in escalated:
        ladder = (inc.run_vars or {}).get("ladder")
        if ladder and not escalation.acknowledged(inc.id):
            escalation.start(inc.id, ladder["roles"], ladder["message"], ladder.get("interval"), start_at=(inc.run_vars or {}).get("ladder_step", 1))
    return resumed


def cancel_all() -> None:
    for t in list(_tasks.values()):
        t.cancel()
    _tasks.clear()
    escalation.cancel_all()
