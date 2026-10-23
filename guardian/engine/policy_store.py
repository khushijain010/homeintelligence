"""Owner tier overrides for policy rules (kept apart so policy.evaluate stays pure)."""

from sqlmodel import select

from guardian import db
from guardian.models import PolicyOverride


def overrides() -> dict[str, str]:
    with db.session() as s:
        return {o.action_type: o.tier for o in s.exec(select(PolicyOverride)).all()}


def set_tier(action_type: str, tier: str, by: str) -> None:
    with db.session() as s:
        row = s.get(PolicyOverride, action_type) or PolicyOverride(action_type=action_type, tier=tier, set_by=by)
        row.tier, row.set_by = tier, by
        s.add(row)
        s.commit()


def clear(action_type: str) -> None:
    with db.session() as s:
        row = s.get(PolicyOverride, action_type)
        if row:
            s.delete(row)
            s.commit()
