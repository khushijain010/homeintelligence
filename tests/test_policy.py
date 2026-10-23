import pytest

from guardian.engine.policy import PolicyContext, Target, evaluate

VALVE = Target("Main Water Valve", online=True, fault=False, fresh=True)
STRONG = dict(confidence=0.85, signal_devices=2, occupancy="empty")


def ctx(action="valve.close", **kw):
    base = {"target": VALVE, **STRONG} if action == "valve.close" else {}
    return PolicyContext(action_type=action, **{**base, **kw})


CASES = [
    # (id, context, expected outcome, substring expected in reasons)
    ("valve auto when strong + empty", ctx(), "allow", "independent device"),
    ("hard deny unlock", ctx("lock.unlock"), "deny", "never allowed"),
    ("hard deny disarm", ctx("alarm.disarm"), "deny", "never allowed"),
    ("hard deny emergency call", ctx("emergency.call"), "deny", "never allowed"),
    ("hard deny even if owner approved", ctx("lock.unlock", owner_approved=True), "deny", "never allowed"),
    ("valve.open denied during water incident", ctx("valve.open", water_incident_open=True), "deny", "water incident"),
    ("unknown action denied", ctx("garage.open"), "deny", "no policy rule"),
    ("notify always auto", ctx("notify.owner"), "allow", "tier auto"),
    ("snapshot auto", ctx("snapshot.capture"), "allow", "tier auto"),
    (
        "offline valve denied",
        ctx(target=Target("Main Water Valve", online=False, fault=False, fresh=True)),
        "deny",
        "offline",
    ),
    (
        "faulted valve denied",
        ctx(target=Target("Main Water Valve", online=True, fault=True, fresh=True)),
        "deny",
        "fault",
    ),
    (
        "stale valve denied",
        ctx(target=Target("Main Water Valve", online=True, fault=False, fresh=False)),
        "deny",
        "not reported recently",
    ),
    ("single signal needs approval", ctx(confidence=0.6, signal_devices=1), "needs_approval", "need 2"),
    ("low confidence needs approval", ctx(confidence=0.55, signal_devices=2), "needs_approval", "need 0.8"),
    ("occupied needs approval", ctx(occupancy="occupied"), "needs_approval", "occupancy is occupied"),
    ("unknown occupancy treated as occupied", ctx(occupancy="unknown"), "needs_approval", "occupancy is unknown"),
    ("rate limited", ctx(prior_same_action=1), "needs_approval", "limit 1"),
    (
        "one signal + owner approval allows",
        ctx(confidence=0.6, signal_devices=1, occupancy="occupied", owner_approved=True),
        "allow",
        "owner approved",
    ),
    (
        "approval does not bypass reliability",
        ctx(target=Target("Main Water Valve", online=False, fault=False, fresh=True), owner_approved=True),
        "deny",
        "offline",
    ),
]


@pytest.mark.parametrize("context,expected,why", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_policy(context, expected, why):
    decision = evaluate(context)
    assert decision.outcome == expected, decision.reasons
    assert any(why in r for r in decision.reasons), decision.reasons


def test_evidence_gate_runs_before_occupancy():
    d = evaluate(ctx(confidence=0.6, signal_devices=1, occupancy="occupied"))
    assert d.outcome == "needs_approval"
    assert "need 2" in d.reasons[-1]
