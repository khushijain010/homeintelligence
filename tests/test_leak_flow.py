from guardian.demo import director
from guardian.devices import registry
from guardian.engine import incidents

from .conftest import wait_for, wait_terminal


def _valve_action(incident_id: str):
    return next(a for a in incidents.actions(incident_id) if a.action_type == "valve.close")


async def test_leak_empty_home_contained(guardian):
    await director.run("leak_empty_home")
    inc = await wait_terminal("water_leak")

    assert inc.status == "contained"
    assert inc.confidence >= 0.8
    action = _valve_action(inc.id)
    assert (action.policy_outcome, action.result, action.verified) == ("allow", "ok", "yes")
    assert registry.all_devices("valve")[0].state["position"] == "closed"
    kinds = {e.kind for e in incidents.evidence(inc.id)}
    assert {"signal", "home_state", "device_health", "policy_decision", "verification"} <= kinds
    assert "Status: contained." in inc.report_text
    assert "Kitchen Leak Sensor" in inc.report_text and "Bathroom Leak Sensor" in inc.report_text


async def test_leak_valve_fault_escalated(guardian):
    await director.run("leak_valve_fault")
    inc = await wait_terminal("water_leak")

    assert inc.status == "escalated"
    action = _valve_action(inc.id)
    assert action.verified == "no"
    assert any("could not confirm" in e.summary for e in incidents.evidence(inc.id))
    owner_alerts = [a for a in incidents.actions(inc.id) if a.action_type == "notify.owner"]
    assert len(owner_alerts) == 2  # initial alert + "could not confirm water is off"
    assert "could not be verified" in inc.report_text


async def test_leak_single_sensor_needs_owner_approval(guardian):
    await director.run("leak_single_sensor")

    def pending():
        for inc in incidents.open_incidents("water_leak"):
            for a in incidents.actions(inc.id):
                if a.action_type == "valve.close" and a.approval_status == "pending":
                    return a
        return None

    action = await wait_for(pending)
    assert action.policy_outcome == "needs_approval"
    resp = await guardian.post(f"/api/actions/{action.id}/approve")
    assert resp.status_code == 200

    inc = await wait_terminal("water_leak")
    assert inc.status == "contained"
    action = _valve_action(inc.id)
    assert (action.approval_status, action.result, action.verified) == ("approved", "ok", "yes")
    assert "(owner approved)" in inc.report_text
