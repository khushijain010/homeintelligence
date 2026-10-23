"""Incident bookkeeping shared by the correlator, runner and API."""

from typing import Any

from sqlmodel import select

from guardian import clock, db, ledger
from guardian.bus import bus
from guardian.devices import registry
from guardian.engine.playbooks import PLAYBOOKS
from guardian.models import Action, Evidence, Incident

TERMINAL = {"contained", "escalated", "resolved", "false_alarm"}


def get(incident_id: str) -> Incident | None:
    with db.session() as s:
        return s.get(Incident, incident_id)


def evidence(incident_id: str) -> list[Evidence]:
    with db.session() as s:
        return list(
            s.exec(select(Evidence).where(Evidence.incident_id == incident_id).order_by(Evidence.created_at, Evidence.id)).all()
        )


def actions(incident_id: str) -> list[Action]:
    with db.session() as s:
        return list(
            s.exec(select(Action).where(Action.incident_id == incident_id).order_by(Action.created_at, Action.id)).all()
        )


def add_evidence(incident_id: str, kind: str, summary: str, payload: dict[str, Any] | None = None) -> Evidence:
    ev = Evidence(incident_id=incident_id, kind=kind, summary=summary, payload=payload or {})
    with db.session() as s:
        s.add(ev)
        s.commit()
    body = {"evidence_id": ev.id, "kind": kind, "summary": summary, "at": ev.created_at}
    if kind == "policy_decision":
        body["reasons"] = (payload or {}).get("reasons")
    ledger.append(f"evidence.{kind}", body, incident_id)
    bus.publish("incident", {"id": incident_id})
    return ev


def update(incident_id: str, **fields: Any) -> Incident | None:
    with db.session() as s:
        inc = s.get(Incident, incident_id)
        if inc is None:  # wiped by a demo reset while its runner was finishing
            return None
        old_status = inc.status
        for k, v in fields.items():
            setattr(inc, k, v)
        if fields.get("status") in TERMINAL and inc.closed_at is None:
            inc.closed_at = clock.now()
        s.add(inc)
        s.commit()
    if "status" in fields and fields["status"] != old_status:
        ledger.append("incident.status", {"from": old_status, "to": fields["status"], "at": clock.now()}, incident_id)
    if fields.get("report_text"):
        ledger.append("incident.report", {"report_text": fields["report_text"], "at": clock.now()}, incident_id)
    bus.publish("incident", {"id": incident_id})
    return inc


def save_action(action: Action) -> Action:
    with db.session() as s:
        s.add(action)
        s.commit()
    ledger.append(
        "action",
        {
            "action_id": action.id,
            "action_type": action.action_type,
            "target_device_id": action.target_device_id,
            "policy_outcome": action.policy_outcome,
            "approval_status": action.approval_status,
            "decided_by": action.decided_by,
            "decided_at": action.decided_at,
            "result": action.result,
            "verified": action.verified,
        },
        action.incident_id,
    )
    bus.publish("incident", {"id": action.incident_id})
    return action


def open_incident(playbook_id: str, title: str) -> Incident:
    pb = PLAYBOOKS[playbook_id]
    inc = Incident(playbook_id=playbook_id, title=title, severity=pb.severity)
    with db.session() as s:
        s.add(inc)
        s.commit()
    ledger.append("incident.opened", {"playbook": playbook_id, "title": title, "at": inc.opened_at}, inc.id)
    bus.publish("incident", {"id": inc.id, "new": True})
    return inc


def open_incidents(playbook_id: str | None = None) -> list[Incident]:
    with db.session() as s:
        q = select(Incident).where(Incident.status.not_in(TERMINAL))  # type: ignore[attr-defined]
        if playbook_id:
            q = q.where(Incident.playbook_id == playbook_id)
        return list(s.exec(q).all())


def signal_devices(incident_id: str) -> list[str]:
    """Distinct devices whose trigger-type signals are attached (independent signals)."""
    seen: list[str] = []
    for ev in evidence(incident_id):
        if ev.kind == "signal" and ev.payload.get("corroborates"):
            dev = ev.payload.get("device_id")
            if dev and dev not in seen:
                seen.append(dev)
    return seen


def recompute_confidence(incident_id: str) -> float:
    """Trigger 0.6; +0.25 per corroborating device; −0.3 per unusable source device;
    plus any evidence-recorded adjustments (e.g. doorbell press before entry −0.2)."""
    devices = signal_devices(incident_id)
    conf = 0.6 + 0.25 * max(len(devices) - 1, 0)
    for dev_id in devices:
        dev = registry.get(dev_id)
        if dev and not registry.reliability(dev)[0]:
            conf -= 0.3
    conf += sum(ev.payload.get("confidence_delta", 0.0) for ev in evidence(incident_id))
    conf = round(min(max(conf, 0.0), 1.0), 2)
    update(incident_id, confidence=conf)
    return conf
