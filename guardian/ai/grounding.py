"""Grounding validator for LLM reports. A report passes only if every sentence is backed by
the timeline items it cites. Returns a list of errors; empty means valid."""

import json
import re
from typing import Any

MAX_WORDS = 120
NUMBER = re.compile(r"\d+(?:[.:]\d+)*")
CITE_IN_TEXT = re.compile(r"\[?\b[EAI]\d+\b\]?")

# (label, pattern). A match is allowed only if the same words appear in the cited items.
FORBIDDEN = [
    ("identity", re.compile(r"\b(identified|recogni[sz]ed?|intruder|burglar|thief|stranger|criminal|suspect)\b", re.I)),
    (
        "cause",
        re.compile(
            r"\b(caused by|root cause|burst|due to|probably|likely|may have|might have|pipes?|appliance failure)\b",
            re.I,
        ),
    ),
    ("damage", re.compile(r"\b(damaged?|damages|destroyed|ruined|costs?|repairs?|insurance|flooded)\b|[₹$€£]\s?\d", re.I)),
]
EMERGENCY = re.compile(
    r"\b(police|fire (?:service|services|brigade|department)|firefighters|ambulance|emergency services)\b", re.I
)
CONTACT_VERB = re.compile(r"\b(notified|called|contacted|alerted|dispatched|informed|summoned)\b", re.I)
NEGATION = re.compile(r"\b(not|never|no|without)\b", re.I)
UNVERIFIED_WORDS = re.compile(
    r"not (?:be )?verified|could not|couldn't|unconfirmed|not confirmed|failed|fault|did not|unverified", re.I
)


def item_text(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False).lower()


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def _number_ok(token: str, source_numbers: set[str]) -> bool:
    if token in source_numbers:
        return True
    # "21:52" is a fair rendering of "21:52:30"
    return ":" in token and any(n.startswith(token + ":") for n in source_numbers)


def _names_in(text: str, names: list[str]) -> list[str]:
    """Known names mentioned in text, longest first so 'Front Door Sensor' doesn't also count as 'Front Door'."""
    found, rest = [], text.lower()
    for name in sorted(names, key=len, reverse=True):
        if name.lower() in rest:
            found.append(name)
            rest = rest.replace(name.lower(), " ")
    return found


def check_text(text: str, source: str, names: list[str], where: str) -> list[str]:
    """Facts in `text` must be present in `source` (already lower-cased item text)."""
    errors = []
    text = CITE_IN_TEXT.sub(" ", text)  # "[E1]" written inline is a citation, not a number
    source_numbers = _numbers(source)
    for token in sorted(_numbers(text)):
        if not _number_ok(token, source_numbers):
            errors.append(f"{where}: number/time {token!r} is not in the cited evidence")
    for name in _names_in(text, names):
        if name.lower() not in source:
            errors.append(f"{where}: {name!r} is not mentioned in the cited evidence")
    for label, pattern in FORBIDDEN:
        for m in pattern.finditer(text):
            if m.group(0).lower() not in source:
                errors.append(f"{where}: unsupported {label} claim {m.group(0)!r}")
    if EMERGENCY.search(text) and CONTACT_VERB.search(text) and not NEGATION.search(text):
        if not (EMERGENCY.search(source) and CONTACT_VERB.search(source)):
            errors.append(f"{where}: claims emergency services were contacted, which no evidence says")
    return errors


def validate(report: Any, timeline: dict[str, Any], names: list[str]) -> list[str]:
    if not isinstance(report, dict):
        return ["report is not a JSON object"]
    errors: list[str] = []
    items = {it["id"]: it for it in timeline["items"]}
    all_text = " ".join(item_text(it) for it in items.values())

    title, summary, sentences = report.get("title"), report.get("summary"), report.get("sentences")
    if not isinstance(title, str) or not title.strip():
        errors.append("missing title")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("missing summary")
    if not isinstance(sentences, list) or not sentences:
        return errors + ["no sentences"]
    if not isinstance(report.get("open_questions", []), list):
        errors.append("open_questions must be a list")

    for field_name, value in (("title", title), ("summary", summary)):
        if isinstance(value, str):
            errors += check_text(value, all_text, names, field_name)

    cited_ids: dict[str, str] = {}  # id -> concatenated sentence texts citing it
    for n, s in enumerate(sentences, 1):
        where = f"sentence {n}"
        if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
            errors.append(f"{where}: missing text")
            continue
        cites = s.get("cites")
        if not isinstance(cites, list) or not cites:
            errors.append(f"{where}: has no citation")
            continue
        unknown = [c for c in cites if c not in items]
        if unknown:
            errors.append(f"{where}: cites unknown id(s) {', '.join(map(str, unknown))}")
            continue
        source = " ".join(item_text(items[c]) for c in cites)
        errors += check_text(s["text"], source, names, where)
        for c in cites:
            cited_ids[c] = cited_ids.get(c, "") + " " + s["text"]

    # an action that ran but wasn't confirmed must be called out plainly
    for it in items.values():
        if it.get("kind") == "action" and it.get("result") and (it.get("verified") != "yes" or it["result"] != "ok"):
            if not UNVERIFIED_WORDS.search(cited_ids.get(it["id"], "")):
                errors.append(f"action {it['id']} ({it['action']}) was not verified but no sentence citing it says so")

    words = len((summary or "").split()) + sum(
        len(s.get("text", "").split()) for s in sentences if isinstance(s, dict) and isinstance(s.get("text"), str)
    )
    if words > MAX_WORDS:
        errors.append(f"report is {words} words; limit is {MAX_WORDS}")
    return errors
