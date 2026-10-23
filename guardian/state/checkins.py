"""Questions for the resident on the Voice Console (simulated Alexa+ check-in).

Alexa+ MCP add-ons are pull-only, so Guardian can't start a spoken conversation on a real
Echo; the console at /resident simulates that and is labelled as such.
"""

import asyncio
import re

from sqlmodel import select

from guardian import clock, db
from guardian.bus import bus
from guardian.models import CheckIn

# order matters: "not okay" must hit HELP before OK sees "okay"
HELP = [r"\bhelp\b", r"\bfell\b", r"\bfall(en)?\b", r"\bpain\b", r"\bhurt", r"\bmadad\b", r"\bdard\b", r"\bgir\b",
        r"not (ok|okay|fine|well|good)", "मदद", "दर्द", "गिर", "बचाओ"]
OK = [r"\bok(ay)?\b", r"\bfine\b", r"\bgood\b", r"\btheek\b", r"\bthik\b", r"\ball right\b", r"\bi am well\b", "ठीक"]
YES = [r"\byes\b", r"\bhaan?\b", r"\bcame\b", r"\baa(y)?i\b", r"\bshe did\b", "हाँ", "हां", "आई"]
NO = [r"\bno\b", r"\bnahi?n?\b", r"\bdidn'?t\b", r"\bdid not\b", r"\bnot come\b", "नहीं", "नही"]


def _match(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(kind: str, transcript: str) -> str | None:
    t = transcript.lower().strip()
    if not t:
        return None
    if kind == "wellbeing":
        if _match(HELP, t) or t in ("no", "nahi", "nahin", "नहीं"):
            return "needs_help"
        if _match(OK, t) or t in ("yes", "haan", "han", "हाँ"):
            return "ok"
        return None
    if kind == "care_confirm":
        if _match(NO, t):
            return "no"
        if _match(YES, t):
            return "yes"
        return None
    return "acknowledged"


VALID = {"wellbeing": {"ok", "needs_help", "no_response"}, "care_confirm": {"yes", "no", "no_response"}, "announcement": {"acknowledged", "no_response"}}


def create(kind: str, prompt_en: str, prompt_hi: str, incident_id: str | None = None, visit_id: str | None = None) -> CheckIn:
    c = CheckIn(kind=kind, prompt_en=prompt_en, prompt_hi=prompt_hi, incident_id=incident_id, visit_id=visit_id)
    with db.session() as s:
        s.add(c)
        s.commit()
    bus.publish("checkin", {"id": c.id, "kind": kind})
    return c


def get(checkin_id: str) -> CheckIn | None:
    with db.session() as s:
        return s.get(CheckIn, checkin_id)


def pending() -> list[CheckIn]:
    with db.session() as s:
        return list(s.exec(select(CheckIn).where(CheckIn.status == "pending").order_by(CheckIn.created_at)).all())  # type: ignore[arg-type]


def latest(kind: str | None = None) -> CheckIn | None:
    with db.session() as s:
        q = select(CheckIn).where(CheckIn.status != "pending")
        if kind:
            q = q.where(CheckIn.kind == kind)
        return s.exec(q.order_by(CheckIn.answered_at.desc())).first()  # type: ignore[union-attr]


def answer(checkin_id: str, answer: str | None = None, transcript: str | None = None) -> str:
    """Record an answer (or classify a transcript). Returns the answer, or 'unclear' (still pending)."""
    c = get(checkin_id)
    if c is None:
        raise KeyError(checkin_id)
    if c.status != "pending":
        return c.answer or "unclear"
    value = answer or (classify(c.kind, transcript or "") if transcript else None)
    if value is None or value not in VALID[c.kind]:
        return "unclear"
    with db.session() as s:
        row = s.get(CheckIn, checkin_id)
        row.status = "answered" if value != "no_response" else "expired"
        row.answer, row.transcript, row.answered_at = value, transcript, clock.now()
        s.add(row)
        s.commit()
    if c.kind == "care_confirm" and c.visit_id and value in ("yes", "no"):
        from guardian.state import care_visits

        care_visits.record_resident_answer(c.visit_id, value)
    bus.publish("checkin", {"id": checkin_id, "answer": value})
    return value


async def wait_for_answer(checkin_id: str, timeout_s: float) -> str:
    deadline = clock.Timer(timeout_s)
    while not deadline.expired():
        c = get(checkin_id)
        if c and c.status != "pending":
            return c.answer or "no_response"
        await asyncio.sleep(0.2)
    return answer(checkin_id, "no_response")
