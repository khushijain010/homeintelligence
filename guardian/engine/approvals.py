"""Owner decisions on pending actions. Used by the dashboard and the Alexa+ MCP tool alike.

Approving only flips the Action to `approved`; the incident's runner then re-runs
policy.evaluate and executes only if the policy still allows it. Nothing here touches an actuator.
"""

from sqlmodel import select

from guardian import clock, db
from guardian.engine import incidents
from guardian.models import Action


class ApprovalError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def pending_actions() -> list[Action]:
    with db.session() as s:
        return list(
            s.exec(select(Action).where(Action.approval_status == "pending").order_by(Action.created_at.desc())).all()  # type: ignore[attr-defined]
        )


def decide(action_id: str | None, approve: bool, by: str) -> Action:
    if action_id is None:
        pending = pending_actions()
        if not pending:
            raise ApprovalError(404, "nothing is waiting for approval")
        if len(pending) > 1:
            raise ApprovalError(409, "more than one action is waiting; say which one")
        action_id = pending[0].id
    with db.session() as s:
        action = s.get(Action, action_id)
    if action is None:
        raise ApprovalError(404, "action not found")
    if action.approval_status != "pending":
        raise ApprovalError(409, f"action is {action.approval_status}, not pending")
    action.approval_status = "approved" if approve else "rejected"
    action.decided_by = by
    action.decided_at = clock.now()
    incidents.save_action(action)
    return action
