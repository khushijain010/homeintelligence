"""Template incident report. Every line is built from stored Evidence / Action rows."""

from zoneinfo import ZoneInfo

from guardian import clock
from guardian.config import settings
from guardian.devices import registry
from guardian.engine import incidents


def _t(dt) -> str:
    return clock.as_utc(dt).astimezone(ZoneInfo(settings.home_timezone)).strftime("%H:%M:%S")


def build(incident_id: str) -> str:
    inc = incidents.get(incident_id)
    evidence = incidents.evidence(incident_id)
    actions = incidents.actions(incident_id)

    signals = [e for e in evidence if e.kind == "signal"]
    home = next((e for e in evidence if e.kind == "home_state"), None)
    decisions = [e for e in evidence if e.kind == "human_decision"]

    lines = [inc.title]
    trigger = signals[0].summary if signals else (evidence[0].summary if evidence else "no trigger recorded")
    lines.append(f"{_t(inc.opened_at)}: {trigger}")
    if home:
        reasons = home.payload.get("reasons") or ["no reason recorded"]
        lines.append(f"Home: {home.payload.get('occupancy')} — {reasons[0]}")
    device_names = []
    for e in signals:
        dev = registry.get(e.payload.get("device_id", ""))
        if dev and dev.name not in device_names:
            device_names.append(dev.name)
    lines.append(f"Evidence: {len(signals)} signals from {', '.join(device_names) or 'no devices'}")
    for a in actions:
        approval = {
            "pending": " (awaiting owner)",
            "approved": " (owner approved)",
            "rejected": " (owner rejected)",
            "expired": " (approval expired)",
        }.get(a.approval_status, "")
        outcome = f"result {a.result}, verified {a.verified}" if a.result else "not run"
        lines.append(f"Actions: {_t(a.created_at)} {a.action_type} — policy {a.policy_outcome}{approval}, {outcome}")
    for d in decisions:
        lines.append(f"Owner decision: {d.summary}")
    for e in evidence:
        if e.payload.get("emergency_services_contacted") is False:
            lines.append(e.summary)
    unverified = [a for a in actions if a.result is not None and a.verified != "yes"]
    warning = f"Warning: {len(unverified)} action(s) could not be verified." if unverified else ""
    lines.append(f"Status: {inc.status}. {warning}".rstrip())
    return "\n".join(lines)
