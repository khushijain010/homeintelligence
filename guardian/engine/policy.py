"""Policy engine: one pure function, no I/O, no LLM. Every action goes through `evaluate`.

Gates run in order and the first failure decides:
1 hard deny, 2 tier, 3 actuator reliability, 4 evidence, 5 occupancy, 6 limits (setpoint, rate).
Gates 4-6 only ever downgrade to needs_approval, which an owner approval satisfies.
Each decision also carries a gate-by-gate trace for the dashboard's "What would happen if…" tester.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["allow", "needs_approval", "deny"]
Tier = Literal["auto", "ask", "never"]

HARD_DENY = {
    "lock.unlock": "Guardian never unlocks doors.",
    "alarm.disarm": "Guardian never disarms alarms.",
    "emergency.call": "Guardian never calls emergency services or claims to have; people do.",
}
# while a smoke/CO incident is open, only these may run (advisory-only playbook)
SMOKE_ALLOWED_PREFIXES = ("notify.", "thermostat.fan_off", "snapshot.capture")


@dataclass(frozen=True)
class Rule:
    tier: Tier
    min_confidence: float | None = None
    min_signal_devices: int | None = None
    requires_occupancy: str | None = None
    max_setpoint: float | None = None
    description: str = ""


DEFAULT_RULES: dict[str, Rule] = {
    "notify.*": Rule("auto", description="Alerts to people (dashboard, SMS/email)"),
    "snapshot.capture": Rule("auto", description="Camera snapshot as evidence"),
    "valve.close": Rule(
        "auto", min_confidence=0.8, min_signal_devices=2, requires_occupancy="empty", description="Shut off the main water valve"
    ),
    "thermostat.set_heat": Rule(
        "auto", min_confidence=0.8, min_signal_devices=2, max_setpoint=20, description="Turn heating on (freeze protection)"
    ),
    "thermostat.fan_off": Rule("auto", description="Stop the HVAC fan (smoke/CO advisory)"),
    "plug.off": Rule("ask", description="Switch off a listed heater plug"),
    "chime.play": Rule("ask", description="Play a warning tone on the Ring chime"),
    "lock.unlock": Rule("never", description=HARD_DENY["lock.unlock"]),
    "alarm.disarm": Rule("never", description=HARD_DENY["alarm.disarm"]),
    "emergency.call": Rule("never", description=HARD_DENY["emergency.call"]),
}
# rows the owner may not change in the dashboard
LOCKED = set(HARD_DENY) | {"notify.*"}


@dataclass(frozen=True)
class Target:
    name: str
    online: bool
    fault: bool
    fresh: bool  # reported within SENSOR_STALE_MINUTES


@dataclass(frozen=True)
class PolicyContext:
    action_type: str
    target: Target | None = None
    confidence: float = 0.0
    signal_devices: int = 0  # independent devices with signals for this incident
    occupancy: str = "unknown"  # empty | occupied | unknown
    water_incident_open: bool = False
    smoke_incident_open: bool = False
    prior_same_action: int = 0  # same action on same device already run in this incident
    owner_approved: bool = False
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)  # [{"gate", "result": pass|fail|skip|approved, "detail"}]


def _rule_for(action_type: str, rules: dict[str, Rule]) -> Rule | None:
    if action_type in rules:
        return rules[action_type]
    return rules.get(action_type.split(".", 1)[0] + ".*")


def evaluate(ctx: PolicyContext, rules: dict[str, Rule] | None = None) -> Decision:
    rules = rules or DEFAULT_RULES
    a = ctx.action_type
    reasons: list[str] = []
    gates: list[dict[str, Any]] = []

    def gate(name: str, result: str, detail: str) -> None:
        gates.append({"gate": name, "result": result, "detail": detail})

    def finish(outcome: Outcome, why: str | None = None) -> Decision:
        return Decision(outcome, reasons + ([why] if why else []), gates)

    # 1. hard deny (not editable)
    if a in HARD_DENY:
        gate("hard deny", "fail", HARD_DENY[a])
        return finish("deny", f"{a} is never allowed")
    if a == "valve.open" and ctx.water_incident_open:
        gate("hard deny", "fail", "valve.open while a water incident is open")
        return finish("deny", "valve.open is never allowed while a water incident is open")
    if ctx.smoke_incident_open and not a.startswith(SMOKE_ALLOWED_PREFIXES):
        gate("hard deny", "fail", "smoke/CO incident open: advisory only")
        return finish("deny", f"{a} is not allowed during a smoke/CO incident (advisory only)")
    gate("hard deny", "pass", "not a forbidden action")

    # 2. tier
    rule = _rule_for(a, rules)
    if rule is None:
        gate("tier", "fail", "no rule")
        return finish("deny", f"no policy rule for {a}")
    if rule.tier == "never":
        gate("tier", "fail", "tier never")
        return finish("deny", f"{a} is tier 'never'")
    gate("tier", "pass", f"tier {rule.tier}")
    reasons.append(f"rule {a}: tier {rule.tier}")

    # 3. actuator reliability (an approval never overrides this)
    if ctx.target is not None:
        t = ctx.target
        for bad, why in ((not t.online, f"{t.name} is offline"), (t.fault, f"{t.name} reports a fault"), (not t.fresh, f"{t.name} has not reported recently")):
            if bad:
                gate("actuator reliability", "fail", why)
                return finish("deny", why)
        gate("actuator reliability", "pass", f"{t.name} healthy")
        reasons.append(f"{t.name} is online and healthy")
    else:
        gate("actuator reliability", "skip", "no device target")

    # 4-6: soft gates, each (name, passed, explanation)
    soft: list[tuple[str, bool, str]] = []
    if rule.tier == "ask":
        soft.append(("tier", False, f"{a} always asks the owner first"))
    if rule.min_confidence is not None or rule.min_signal_devices is not None:
        min_conf, min_dev = rule.min_confidence or 0.0, rule.min_signal_devices or 0
        ok = ctx.confidence >= min_conf and ctx.signal_devices >= min_dev
        soft.append(
            (
                "evidence",
                ok,
                f"confidence {ctx.confidence:.2f} (need {min_conf}) from "
                f"{ctx.signal_devices} independent device(s) (need {min_dev})",
            )
        )
    if rule.requires_occupancy:
        soft.append(("occupancy", ctx.occupancy == rule.requires_occupancy, f"occupancy is {ctx.occupancy} (rule requires {rule.requires_occupancy})"))
    if rule.max_setpoint is not None and "setpoint_c" in ctx.params:
        sp = float(ctx.params["setpoint_c"])
        soft.append(("limits", sp <= rule.max_setpoint, f"setpoint {sp:g} °C (auto limit {rule.max_setpoint:g} °C)"))
    if ctx.target is not None:  # rate limit: device actions run once per incident (notifications are not limited)
        soft.append(("limits", ctx.prior_same_action == 0, f"{a} on {ctx.target.name} ran {ctx.prior_same_action} time(s) before (limit 1)"))

    for name, ok, why in soft:
        if ok:
            gate(name, "pass", why)
            reasons.append(why)
        elif ctx.owner_approved:
            gate(name, "approved", why)
            reasons.append(f"{why} — owner approved")
        else:
            gate(name, "fail", why)
            return finish("needs_approval", why)
    for name in ("evidence", "occupancy", "limits"):
        if not any(g["gate"] == name for g in gates):
            gate(name, "skip", "no condition for this action")
    return finish("allow")


# --------------------------------------------------------------------------- owner overrides (Module 7)


def current_rules() -> dict[str, Rule]:
    """Default rules with the owner's tier overrides (locked rows can't change)."""
    from dataclasses import replace

    from guardian.engine import policy_store

    rules = dict(DEFAULT_RULES)
    for action_type, tier in policy_store.overrides().items():
        if action_type in rules and action_type not in LOCKED and tier in ("auto", "ask", "never"):
            rules[action_type] = replace(rules[action_type], tier=tier)
    return rules
