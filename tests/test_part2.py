"""Part 2 checks: routine (M3), playbooks (M4), ledger (M5), SNS (M6), policy UI (M7), reliability (M8)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from guardian import clock, db, ledger
from guardian.demo import director, history
from guardian.devices import monitor, registry, virtual
from guardian.engine import incidents, policy, runner
from guardian.models import Action, LedgerEntry, Signal
from guardian.notify import sns
from guardian.state import checkins, routine

from .conftest import wait_for, wait_terminal


def latest(playbook_id):
    rows = [i for i in incidents_all() if i.playbook_id == playbook_id]
    return rows[-1] if rows else None


def incidents_all():
    from guardian.models import Incident

    with db.session() as s:
        return list(s.exec(select(Incident).order_by(Incident.opened_at)).all())


# --------------------------------------------------------------------------- Module 3: routine


async def test_baseline_math_from_seeded_history(guardian, monkeypatch):
    monkeypatch.setattr(director, "SEED_HISTORY", True)
    await director.reset(include_today=False)
    b = routine.baseline()
    assert b.learned_days == 14 and routine.learning_status()["ready"]
    # first activity ≈ 06:40 walk (±6 min jitter, person seen 20 s before) → p90 between 06:33 and 06:47
    assert 6 * 60 + 33 <= b.first_activity_p90_min <= 6 * 60 + 47
    # longest Kamla outing: evening walk ~60 min (±8 min each side), caregiver door events excluded
    assert 44 <= b.longest_outing_min <= 76
    morning_slot = (6 * 60 + 30) // 30
    assert max(b.slots[str(w)][morning_slot] for w in range(7)) == 1.0
    assert all(v == 0.0 for w in range(7) for v in b.slots[str(w)][0:10])  # nothing 00:00–05:00


def test_history_plan_keeps_episodes_whole():
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)  # 17:30 IST: evening walk not finished yet
    events = history.plan(days=0, include_today=True, now=now)
    assert all(e[0] < now for e in events)
    times = [e[0].astimezone(routine.tz()).hour for e in events]
    assert 17 not in times  # walk out at 17:00 without its return is not seeded


async def test_missed_morning_checks_in_then_alerts_family(guardian, monkeypatch):
    monkeypatch.setattr(director, "SEED_HISTORY", True)
    monkeypatch.setattr(runner.settings, "checkin_timeout_seconds", 1.0)
    await director.run("missed_morning")
    c = await wait_for(lambda: checkins.pending()[-1] if checkins.pending() else None)
    assert c.kind == "wellbeing" and "Kamla" in c.prompt_en
    inc = await wait_terminal("missed_routine")  # no answer within the (shortened) 3 min
    assert inc.status == "escalated"
    family = [a for a in incidents.actions(inc.id) if a.action_type == "notify.family"]
    assert family and "Last front-door activity" in family[0].policy_reasons[0] or family
    assert any("No answer from Kamla" in e.summary for e in incidents.evidence(inc.id))


async def test_missed_morning_ok_answer_is_false_alarm(guardian, monkeypatch):
    monkeypatch.setattr(director, "SEED_HISTORY", True)
    await director.run("missed_morning")
    c = await wait_for(lambda: checkins.pending()[-1] if checkins.pending() else None)
    resp = await guardian.post(f"/api/checkins/{c.id}/answer", json={"transcript": "haan main theek hoon"})
    assert resp.json()["result"] == "ok"
    inc = await wait_terminal("missed_routine")
    assert inc.status == "false_alarm"
    assert not [a for a in incidents.actions(inc.id) if a.action_type == "notify.family"]


def test_checkin_keywords():
    assert checkins.classify("wellbeing", "I'm fine") == "ok"
    assert checkins.classify("wellbeing", "I am not okay") == "needs_help"
    assert checkins.classify("wellbeing", "madad karo, gir gayi") == "needs_help"
    assert checkins.classify("wellbeing", "what?") is None
    assert checkins.classify("care_confirm", "nahi aayi") == "no"
    assert checkins.classify("care_confirm", "haan aayi thi") == "yes"


async def test_deviation_rules(guardian, monkeypatch):
    monkeypatch.setattr(director, "SEED_HISTORY", True)
    await director.reset(include_today=False)
    today = routine.local(clock.now()).date()
    b = routine.baseline()
    early = routine.midnight(today) + timedelta(minutes=b.first_activity_p90_min + 10)
    assert not [d for d in routine.evaluate(early) if d["rule"] == "no_morning_activity"]
    late = routine.midnight(today) + timedelta(minutes=b.first_activity_p90_min + 46)
    assert [d for d in routine.evaluate(late) if d["rule"] == "no_morning_activity"]

    # night exit: door opens at 23:10 and nothing follows within 20 min
    door = registry.by_external_id(director.seed.DEVICE_IDS["Front Door Sensor"])
    t = routine.midnight(today) + timedelta(hours=23, minutes=10)
    registry.record_signal(door, "contact.opened", observed_at=t)
    assert [d for d in routine.evaluate(t + timedelta(minutes=25)) if d["rule"] == "night_exit"]

    # not returned: went out at 13:00 (one opening), longest outing + 90 min later
    t2 = routine.midnight(today) + timedelta(hours=13)
    registry.record_signal(door, "contact.opened", observed_at=t2)
    later = t2 + timedelta(minutes=b.longest_outing_min + 91)
    assert [d for d in routine.evaluate(later) if d["rule"] == "not_returned"]

    # pauses suppress everything
    clock.freeze(late)
    routine.pause(4)
    assert routine.evaluate(late) == []


async def test_care_answer_contradicting_camera_is_unverified(guardian):
    from guardian.models import CareSchedule, CareVisit
    from guardian.state import care_visits

    with db.session() as s:
        sch = s.exec(select(CareSchedule)).first()
        v = CareVisit(schedule_id=sch.id, date="2026-10-19", status="completed", camera_status="completed", arrived_at=clock.now())
        s.add(v)
        s.commit()
    care_visits.record_resident_answer(v.id, "no")
    with db.session() as s:
        v = s.get(CareVisit, v.id)
    assert (v.status, v.camera_status, v.resident_answer) == ("unverified", "completed", "no")


# --------------------------------------------------------------------------- Module 4: playbooks


async def test_freeze_night_heats_and_verifies(guardian):
    await director.run("freeze_night")
    inc = await wait_terminal("temperature_risk", timeout=20)
    assert inc.status == "contained"
    heat = next(a for a in incidents.actions(inc.id) if a.action_type == "thermostat.set_heat")
    assert (heat.policy_outcome, heat.result, heat.verified) == ("allow", "ok", "yes")
    assert any("Temperature rising" in e.summary for e in incidents.evidence(inc.id))


async def test_smoke_advisory_only_fan_off_and_no_emergency_claim(guardian):
    await director.run("smoke_advisory")
    inc = await wait_terminal("smoke_co")
    assert inc.status == "escalated"
    kinds = {a.action_type for a in incidents.actions(inc.id)}
    assert "thermostat.fan_off" in kinds and not kinds & {"valve.close", "plug.off", "emergency.call"}
    assert "did not contact emergency services" in inc.report_text
    # while it is open-or-just-closed, policy refuses anything else
    d = policy.evaluate(policy.PolicyContext("valve.close", smoke_incident_open=True))
    assert d.outcome == "deny"


async def test_device_offline_degrades_protection_and_open_incident(guardian):
    await director.run("leak_single_sensor")
    leak = await wait_for(lambda: incidents.open_incidents("water_leak") or None)
    kitchen = registry.by_external_id(director.seed.DEVICE_IDS["Kitchen Leak Sensor"])
    before = incidents.get(leak[0].id).confidence
    registry.record_signal(kitchen, "device.offline")
    health = await wait_terminal("device_health")
    assert "leak detection in Kitchen" in health.report_text or any("leak detection in Kitchen" in e.summary for e in incidents.evidence(health.id))
    assert incidents.get(leak[0].id).confidence < before


def test_policy_new_rules():
    p = policy.PolicyContext
    assert policy.evaluate(p("chime.play")).outcome == "needs_approval"
    assert policy.evaluate(p("plug.off")).outcome == "needs_approval"
    assert policy.evaluate(p("thermostat.fan_off")).outcome == "allow"
    strong = dict(confidence=0.85, signal_devices=2)
    assert policy.evaluate(p("thermostat.set_heat", params={"setpoint_c": 16}, **strong)).outcome == "allow"
    hot = policy.evaluate(p("thermostat.set_heat", params={"setpoint_c": 25}, **strong))
    assert hot.outcome == "needs_approval" and "auto limit 20" in hot.reasons[-1]
    gates = {g["gate"]: g["result"] for g in hot.gates}
    assert gates["hard deny"] == "pass" and gates["limits"] == "fail"


async def test_policy_editor_cannot_unlock_hard_denies(guardian):
    assert (await guardian.put("/api/policy/rules/lock.unlock", json={"tier": "auto"})).status_code == 403
    assert (await guardian.put("/api/policy/rules/valve.close", json={"tier": "ask"})).status_code == 200
    assert policy.current_rules()["valve.close"].tier == "ask"
    r = (await guardian.post("/api/policy/evaluate", json={"action_type": "valve.close"})).json()
    assert r["outcome"] == "needs_approval"


# --------------------------------------------------------------------------- Module 5: ledger


async def test_ledger_chain_verifies_and_detects_tampering(guardian):
    await director.run("leak_valve_fault")
    await wait_terminal("water_leak")
    assert ledger.verify()["ok"]
    kinds = {e.kind for e in ledger.entries(500)}
    assert {"signal", "action", "incident.opened", "incident.status", "incident.report", "evidence.policy_decision"} <= kinds
    with db.session() as s:
        target = s.exec(select(LedgerEntry).where(LedgerEntry.kind == "action")).first()
        target.body = {**target.body, "result": "ok"}  # rewrite history
        s.add(target)
        s.commit()
    out = (await guardian.get("/api/ledger/verify")).json()
    assert out == {"ok": False, "broken_seq": target.seq, "reason": "body or hash was altered"}


# --------------------------------------------------------------------------- Module 6: SNS


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, TopicArn, Message, Subject):
        self.sent.append((TopicArn, Subject, Message))
        return {"MessageId": f"m{len(self.sent)}"}


async def test_sns_alerts_on_critical_and_approval(guardian, monkeypatch):
    monkeypatch.setattr(sns.settings, "sns_topic_arn", "arn:aws:sns:ap-south-1:1:guardian")
    monkeypatch.setattr(sns.settings, "sns_topic_arn_family", "arn:aws:sns:ap-south-1:1:family")
    fake = FakeSNS()
    sns.set_client(fake)
    try:
        await director.run("leak_single_sensor")
        await wait_for(lambda: len(fake.sent) >= 2)
        subjects = " ".join(m for _, _, m in fake.sent)
        assert "Approval needed to shut off the water" in subjects
        assert all("#incident-" in m for _, _, m in fake.sent)
        inc = incidents.open_incidents("water_leak")[0]
        assert any("Amazon SNS" in e.summary for e in incidents.evidence(inc.id))
        assert sns.topic_for("family").endswith(":family") and sns.topic_for("neighbor").endswith(":guardian")
    finally:
        sns.set_client(None)


def test_sns_off_without_config():
    sns.set_client(None)
    assert sns.publish("owner", "hello") is None


async def test_escalation_ladder_until_ack(guardian, monkeypatch):
    monkeypatch.setattr(runner.settings, "escalation_interval_seconds", 0.3)
    await director.run("leak_valve_fault")
    inc = await wait_terminal("water_leak")
    await wait_for(lambda: any(a.action_type == "notify.neighbor" for a in incidents.actions(inc.id)), timeout=5)
    assert (await guardian.post(f"/api/incidents/{inc.id}/ack")).status_code == 200
    n = len(incidents.actions(inc.id))
    import asyncio

    await asyncio.sleep(1.0)
    assert len(incidents.actions(inc.id)) == n  # stopped after acknowledgement


# --------------------------------------------------------------------------- Module 8: reliability


async def test_resume_does_not_repeat_actions(guardian):
    """Simulate a crash mid-wait: cancel the runner, then resume_all; valve.close runs exactly once."""
    await director.run("leak_single_sensor")
    action = await wait_for(
        lambda: next((a for i in incidents.open_incidents("water_leak") for a in incidents.actions(i.id) if a.approval_status == "pending"), None)
    )
    runner.cancel_all()  # Guardian "dies" while waiting for approval
    await guardian.post(f"/api/actions/{action.id}/approve")
    assert action.incident_id in runner.resume_all()
    inc = await wait_terminal("water_leak")
    assert inc.status == "contained"
    closes = [a for a in incidents.actions(inc.id) if a.action_type == "valve.close"]
    assert len(closes) == 1 and closes[0].idempotency_key.startswith(inc.id)
    assert any("resumed" in e.summary for e in incidents.evidence(inc.id))


def test_clock_speed_scales_timers():
    clock.reset()
    clock.set_speed(60)
    t = clock.Timer(60)  # one clock-minute = one real second
    assert 0 < t.remaining() <= 60
    clock.reset()
    assert clock.speed() == 1.0


async def test_mcp_wellbeing_tool(guardian, monkeypatch):
    from .conftest import call_tool

    monkeypatch.setattr(director, "SEED_HISTORY", True)
    await director.reset()
    out = await call_tool(guardian, "get_resident_wellbeing")
    assert "Kamla" in out["speech"] and len(out["speech"].split()) <= 60
