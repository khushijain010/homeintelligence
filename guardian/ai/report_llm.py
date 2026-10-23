"""LLM-written incident report, accepted only if the grounding validator passes.
The model only ever produces text; nothing it says reaches the policy engine or an actuator."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import select

from guardian import clock, db
from guardian.ai import bedrock, grounding
from guardian.config import settings
from guardian.devices import registry
from guardian.engine import incidents
from guardian.models import IncidentReport, Resident

log = logging.getLogger("guardian.report_llm")
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "report.md").read_text(encoding="utf-8")

# payload keys worth showing the model, per evidence kind (never images)
_DETAIL_KEYS = {
    "policy_decision": ("outcome", "reasons", "approval"),
    "verification": ("check", "position", "passed", "seconds"),
    "home_state": ("mode", "occupancy", "reasons"),
    "human_decision": ("choice", "approval"),
    "signal": ("type",),
}


def _t(dt) -> str:
    return clock.as_utc(dt).astimezone(ZoneInfo(settings.home_timezone)).strftime("%H:%M:%S")


def build_timeline(incident_id: str) -> tuple[dict[str, Any], dict[str, dict]]:
    """Compact timeline with short ids, plus a map from id to the DB row it came from."""
    inc = incidents.get(incident_id)
    items: list[dict[str, Any]] = [
        {
            "id": "I1",
            "kind": "incident",
            "title": inc.title,
            "status": inc.status,
            "opened": _t(inc.opened_at),
            "confidence": inc.confidence,
        }
    ]
    refs: dict[str, dict] = {"I1": {"type": "incident", "id": inc.id}}
    rows = [("E", e.created_at, e) for e in incidents.evidence(incident_id)]
    rows += [("A", a.created_at, a) for a in incidents.actions(incident_id)]
    rows.sort(key=lambda r: (clock.as_utc(r[1]), r[2].id))
    counters = {"E": 0, "A": 0}
    for prefix, _, row in rows:
        counters[prefix] += 1
        rid = f"{prefix}{counters[prefix]}"
        if prefix == "E":
            item = {"id": rid, "kind": row.kind, "time": _t(row.created_at), "text": row.summary}
            item.update({k: row.payload[k] for k in _DETAIL_KEYS.get(row.kind, ()) if k in row.payload})
            refs[rid] = {"type": "evidence", "id": row.id}
        else:
            target = registry.get(row.target_device_id) if row.target_device_id else None
            item = {
                "id": rid,
                "kind": "action",
                "time": _t(row.created_at),
                "action": row.action_type,
                "target": target.name if target else None,
                "policy": row.policy_outcome,
                "approval": row.approval_status,
                "result": row.result,
                "verified": row.verified,
            }
            refs[rid] = {"type": "action", "id": row.id}
        items.append(item)
    return {"items": items}, refs


def known_names() -> list[str]:
    with db.session() as s:
        residents = [r.name for r in s.exec(select(Resident)).all()]
    return [d.name for d in registry.all_devices()] + residents


def _parse(raw: str) -> Any:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    return json.loads(raw[start : end + 1])


def _save(incident_id: str, **fields: Any) -> IncidentReport:
    rep = IncidentReport(incident_id=incident_id, **fields)
    with db.session() as s:
        s.add(rep)
        s.commit()
    return rep


async def generate(incident_id: str) -> IncidentReport | None:
    """Try for a validated LLM report (one retry with the errors). None → use the template."""
    client = bedrock.get_client()
    if client is None:
        return None
    timeline, refs = build_timeline(incident_id)
    names = known_names()
    messages: list[dict] = [{"role": "user", "content": [{"text": "Timeline:\n" + json.dumps(timeline)}]}]
    all_errors: list[str] = []
    for attempt in range(2):
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(client.converse, SYSTEM_PROMPT, messages),
                timeout=settings.bedrock_timeout_seconds + 2,
            )
        except Exception as exc:  # timeout, throttling, auth: fall back, never fail the incident
            log.warning("Bedrock call failed for incident %s: %s", incident_id, exc)
            all_errors.append(f"attempt {attempt + 1}: Bedrock call failed: {exc}")
            break
        try:
            report = _parse(raw)
            errors = grounding.validate(report, timeline, names)
        except ValueError as exc:
            report, errors = None, [f"reply is not valid JSON: {exc}"]
        if not errors:
            return _save(
                incident_id,
                source="bedrock",
                model_id=getattr(client, "model_id", None),
                title=report["title"],
                summary=report["summary"],
                sentences=[{"text": s["text"], "cites": s["cites"]} for s in report["sentences"]],
                open_questions=[str(q) for q in report.get("open_questions", [])],
                refs=refs,
                errors=all_errors,
            )
        all_errors += [f"attempt {attempt + 1}: {e}" for e in errors]
        messages += [
            {"role": "assistant", "content": [{"text": raw}]},
            {
                "role": "user",
                "content": [
                    {
                        "text": "Your report failed validation:\n- "
                        + "\n- ".join(errors)
                        + "\nReturn corrected JSON only, using only facts from the cited items."
                    }
                ],
            },
        ]
    log.info("LLM report rejected for incident %s; using template", incident_id)
    _save(incident_id, source="template", model_id=getattr(client, "model_id", None), refs=refs, errors=all_errors)
    return None


def latest(incident_id: str) -> IncidentReport | None:
    with db.session() as s:
        return s.exec(
            select(IncidentReport)
            .where(IncidentReport.incident_id == incident_id)
            .order_by(IncidentReport.created_at.desc(), IncidentReport.id.desc())  # type: ignore[attr-defined]
        ).first()
