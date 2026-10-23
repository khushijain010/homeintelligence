"""Hash-chained evidence ledger (append-only).

hash = SHA-256(prev_hash + canonical JSON body). Entries are written for every signal, incident
change, policy decision, approval (who and when), action, verification and report. Editing or
deleting any row breaks verification at that row's seq.
"""

import hashlib
import json
import threading
from typing import Any

from sqlmodel import select

from guardian import clock, db
from guardian.models import LedgerEntry

GENESIS = "0" * 64
_lock = threading.Lock()  # sync API routes run in worker threads


def canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def chain_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(body)).encode()).hexdigest()


def append(kind: str, body: dict[str, Any], incident_id: str | None = None) -> LedgerEntry:
    body = json.loads(canonical(body))  # freeze to plain JSON so the stored body hashes identically
    with _lock, db.session() as s:
        last = s.exec(select(LedgerEntry).order_by(LedgerEntry.seq.desc())).first()  # type: ignore[union-attr]
        prev = last.hash if last else GENESIS
        entry = LedgerEntry(
            seq=(last.seq + 1) if last else 1,
            incident_id=incident_id,
            kind=kind,
            body=body,
            prev_hash=prev,
            hash=chain_hash(prev, body),
            created_at=clock.now(),
        )
        s.add(entry)
        s.commit()
        return entry


def entries(limit: int = 100, incident_id: str | None = None) -> list[LedgerEntry]:
    with db.session() as s:
        q = select(LedgerEntry)
        if incident_id:
            q = q.where(LedgerEntry.incident_id == incident_id)
        return list(s.exec(q.order_by(LedgerEntry.seq.desc()).limit(limit)).all())  # type: ignore[union-attr]


def verify() -> dict[str, Any]:
    """{"ok": True, "entries": n} or {"ok": False, "broken_seq": seq, "reason": ...}."""
    prev, expected_seq, n = GENESIS, 1, 0
    with db.session() as s:
        for e in s.exec(select(LedgerEntry).order_by(LedgerEntry.seq)).all():  # type: ignore[arg-type]
            n += 1
            if e.seq != expected_seq:
                return {"ok": False, "broken_seq": expected_seq, "reason": "entry missing"}
            if e.prev_hash != prev:
                return {"ok": False, "broken_seq": e.seq, "reason": "prev_hash does not match the previous entry"}
            if chain_hash(prev, e.body) != e.hash:
                return {"ok": False, "broken_seq": e.seq, "reason": "body or hash was altered"}
            prev, expected_seq = e.hash, expected_seq + 1
    return {"ok": True, "entries": n}
