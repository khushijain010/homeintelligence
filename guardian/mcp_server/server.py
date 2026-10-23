"""Alexa+ MCP server (Streamable HTTP at /mcp).

Every tool is a fast DB read (no LLM calls) and returns {"speech": <60 words, "data": {...}}.
approve_action only records the owner's decision; the incident runner re-runs
policy.evaluate before anything is executed.
"""

from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from sqlmodel import select

from guardian import clock, db
from guardian.ai import report_llm
from guardian.config import settings
from guardian.devices import registry
from guardian.engine import approvals, incidents
from guardian.models import Action, CareSchedule, CareVisit, Incident, Resident
from guardian.oauth.server import caller, has_scope
from guardian.state import home_state

MAX_WORDS = 60

server = MCPServer(
    "Guardian",
    title="Guardian home safety",
    instructions=(
        "Guardian watches a family home with Ring devices. Use these tools to report home status, "
        "incidents, incident reports and caregiver visits, to change the home mode, and to pass on the "
        "owner's approval or rejection of an action Guardian proposed. Guardian's safety policy still "
        "decides whether an approved action runs."
    ),
)


def speech(text: str) -> str:
    words = text.split()
    return text if len(words) <= MAX_WORDS else " ".join(words[:MAX_WORDS]).rstrip(",.;") + "…"


class ToolReply(BaseModel):
    speech: str  # what Alexa says, under 60 words
    data: dict[str, Any]


def reply(text: str, **data) -> ToolReply:
    return ToolReply(speech=speech(text), data=data)


def _pending_for(incident_id: str) -> list[Action]:
    return [a for a in incidents.actions(incident_id) if a.approval_status == "pending"]


ACTION_WORDS = {"valve.close": "shut off the water", "chime.play": "sound the chime warning", "plug.off": "turn off the heater plug"}


@server.tool()
async def get_home_status() -> ToolReply:
    """How things are at home: mode, whether anyone seems to be home, open incidents, and devices that need attention."""
    snap = home_state.snapshot()
    open_ = incidents.open_incidents()
    degraded = []
    for d in registry.all_devices():
        usable, problems = registry.reliability(d)
        if not usable:
            degraded.append({"name": d.name, "problems": problems})
    parts = [f"Guardian has the home in {snap['mode']} mode, and it looks {snap['occupancy']}."]
    if open_:
        names = "; ".join(f"{i.title}, {i.status.replace('_', ' ')}" for i in open_[:3])
        parts.append(f"{len(open_)} open incident{'s' if len(open_) > 1 else ''}: {names}.")
    else:
        parts.append("There are no open incidents.")
    if degraded:
        parts.append("Needs attention: " + ", ".join(d["name"] for d in degraded[:3]) + ".")
    else:
        parts.append("All devices are working.")
    return reply(
        " ".join(parts),
        mode=snap["mode"],
        occupancy=snap["occupancy"],
        occupancy_reasons=snap["reasons"],
        open_incidents=[{"id": i.id, "title": i.title, "status": i.status} for i in open_],
        degraded_devices=degraded,
    )


@server.tool()
async def list_open_incidents() -> ToolReply:
    """List up to five open incidents, newest first, with any actions waiting for approval."""
    open_ = sorted(incidents.open_incidents(), key=lambda i: clock.as_utc(i.opened_at), reverse=True)[:5]
    if not open_:
        return reply("There are no open incidents right now.", incidents=[])
    items, phrases = [], []
    for i in open_:
        pending = _pending_for(i.id)
        items.append(
            {
                "id": i.id,
                "title": i.title,
                "status": i.status,
                "severity": i.severity,
                "pending_actions": [{"id": a.id, "action_type": a.action_type} for a in pending],
            }
        )
        extra = f", waiting for your approval to {ACTION_WORDS.get(pending[0].action_type, pending[0].action_type)}" if pending else ""
        phrases.append(f"{i.title}, {i.status.replace('_', ' ')}{extra}")
    return reply(f"{len(open_)} open: " + "; ".join(phrases) + ".", incidents=items)


@server.tool()
async def get_incident_report(incident_id: str | None = None) -> ToolReply:
    """The report for an incident (the latest one if no id is given). Reports only state recorded evidence."""
    with db.session() as s:
        if incident_id:
            inc = s.get(Incident, incident_id)
        else:
            inc = s.exec(select(Incident).order_by(Incident.opened_at.desc())).first()  # type: ignore[attr-defined]
    if inc is None:
        return reply("I couldn't find that incident." if incident_id else "There have been no incidents.", found=False)
    llm = report_llm.latest(inc.id)
    if llm and llm.source == "bedrock":
        text = f"{llm.summary} " + " ".join(s["text"] for s in llm.sentences)
        source = "validated_ai"
    elif inc.report_text:
        lines = inc.report_text.splitlines()
        text = f"{inc.title}. {lines[1].split(': ', 1)[-1]}. Status: {inc.status.replace('_', ' ')}."
        source = "template"
    else:
        text = f"{inc.title} is still {inc.status.replace('_', ' ')}; the report isn't written yet."
        source = "none"
    return reply(text, found=True, incident_id=inc.id, status=inc.status, source=source, report_text=inc.report_text)


@server.tool()
async def approve_action(decision: Literal["approve", "reject"], action_id: str | None = None) -> ToolReply:
    """Pass on the owner's decision for an action Guardian is waiting on (for example shutting off the water).
    If action_id is omitted, the single pending action is used. Guardian re-checks its safety policy before acting."""
    if not has_scope("guardian.act"):
        return reply("This Alexa link only allows reading. Re-link Guardian with permission to act.", ok=False)
    try:
        action = approvals.decide(action_id, decision == "approve", by=caller())
    except approvals.ApprovalError as exc:
        return reply(f"I couldn't do that: {exc}.", ok=False)
    what = ACTION_WORDS.get(action.action_type, action.action_type)
    if decision == "approve":
        text = f"Approved. Guardian will re-check its safety rules, then {what} and confirm it worked."
    else:
        text = f"Okay, Guardian will not {what}. The incident stays escalated to you."
    return reply(text, ok=True, action_id=action.id, approval_status=action.approval_status)


@server.tool()
async def set_home_mode(mode: Literal["home", "away", "night"]) -> ToolReply:
    """Set the home mode: home, away or night."""
    if not has_scope("guardian.act"):
        return reply("This Alexa link only allows reading. Re-link Guardian with permission to act.", ok=False)
    home_state.set_mode(mode)
    return reply(f"Done. Guardian has the home in {mode} mode.", ok=True, mode=mode)


@server.tool()
async def get_care_visits(days: int = 7) -> ToolReply:
    """Caregiver visits over the last few days (default 7), as seen at the front door."""
    days = max(1, min(days, 31))
    since = (clock.now() - timedelta(days=days)).date().isoformat()
    with db.session() as s:
        visits = s.exec(select(CareVisit).where(CareVisit.date >= since).order_by(CareVisit.date.desc())).all()  # type: ignore[attr-defined]
        rows = []
        for v in visits:
            sch = s.get(CareSchedule, v.schedule_id)
            who = s.get(Resident, sch.caregiver_id) if sch else None
            rows.append({"date": v.date, "caregiver": who.name if who else "caregiver", "status": v.status, "minutes": v.duration_min})
    if not rows:
        return reply(f"No caregiver visits were checked in the last {days} days.", visits=[])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    order = ["completed", "late", "short", "no_show", "unverified"]
    summary = ", ".join(f"{counts[k]} {k.replace('_', '-')}" for k in order if k in counts)
    latest = rows[0]
    text = f"In the last {days} days: {summary}. Most recently, {latest['date']}, {latest['caregiver']}'s visit was {latest['status'].replace('_', ' ')}."
    return reply(text, visits=rows)


@server.tool()
async def get_resident_wellbeing() -> ToolReply:
    """How the resident (Mom) is doing today: last front-door activity, routine, check-ins and pauses."""
    from guardian.state import checkins, routine

    with db.session() as s:
        elder = s.exec(select(Resident).where(Resident.role == "resident_elder")).first()
    name = elder.name if elder else "The resident"
    status = routine.learning_status()
    last = routine.describe_last_activity()
    now = clock.now()
    today = routine.local(now).date()
    today_count = len(routine.activity(routine.midnight(today), now))
    open_ = incidents.open_incidents("missed_routine")
    pause = routine.active_pause()
    answer = checkins.latest("wellbeing")
    parts = [f"{name}'s last front-door activity: {last}."]
    if not status["ready"]:
        parts.append(f"Guardian is still learning her routine ({status['learned_days']} of 7 days).")
    elif open_:
        parts.append("Her day looks unusual and Guardian is checking on her.")
    else:
        parts.append(f"Her routine looks normal today, with {today_count} front-door events so far.")
    if answer and answer.answered_at and routine.local(answer.answered_at).date() == today:
        said = {"ok": "She told Guardian she is okay.", "needs_help": "She asked for help.", "no_response": "She missed a check-in."}
        parts.append(said.get(answer.answer, ""))
    if pause:
        parts.append(f"She paused monitoring until {routine.local(pause.until).strftime('%H:%M')}.")
    return reply(
        " ".join(p for p in parts if p),
        last_activity=last,
        learning=status,
        today_events=today_count,
        open_routine_incidents=[{"id": i.id, "title": i.title, "status": i.status} for i in open_],
        latest_checkin=answer.answer if answer else None,
        paused_until=clock.as_utc(pause.until).isoformat() if pause else None,
    )


def build_app():
    """Fresh Streamable HTTP endpoint + session manager (a manager can run only once)."""
    host = urlparse(settings.public_base_url).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[settings.public_base_url.rstrip("/"), "http://127.0.0.1:*", "http://localhost:*"],
    )
    starlette_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    endpoint = next(r.endpoint for r in starlette_app.routes if getattr(r, "path", "") == "/mcp")
    return endpoint, server.session_manager
