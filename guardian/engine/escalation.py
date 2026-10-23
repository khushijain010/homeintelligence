"""Escalation ladder: keep alerting people, role by role, until someone acknowledges.

Every alert is a `notify.<role>` action through the policy engine (with an idempotency key),
so the ladder can be resumed after a restart without sending duplicates.
"""

import asyncio
import logging

from guardian import clock
from guardian.config import settings
from guardian.engine import incidents

log = logging.getLogger("guardian.escalation")
_tasks: dict[str, asyncio.Task] = {}


def acknowledged(incident_id: str) -> bool:
    return any(e.kind == "human_decision" and e.payload.get("ack") for e in incidents.evidence(incident_id))


def acknowledge(incident_id: str, by: str) -> bool:
    if acknowledged(incident_id):
        return False
    incidents.add_evidence(incident_id, "human_decision", f"Acknowledged by {by}", {"ack": True, "by": by})
    task = _tasks.pop(incident_id, None)
    if task:
        task.cancel()
    return True


async def _ladder(incident_id: str, roles: list[str], interval: float, message: str, max_rounds: int, start_at: int) -> None:
    from guardian.engine import runner  # runner imports this module

    step = start_at
    total = len(roles) * max_rounds
    while step < total:
        await clock.sleep(interval)
        if acknowledged(incident_id) or incidents.get(incident_id) is None:
            return
        role = roles[step % len(roles)]
        round_no = step // len(roles) + 1
        await runner.notify_role(
            incident_id,
            role,
            f"{message} (reminder {step}; not yet acknowledged)",
            key=f"{incident_id}:ladder:{step}:{role}",
        )
        incidents.add_evidence(
            incident_id,
            "policy_decision",
            f"Escalation ladder: alerted {role} (round {round_no}); waiting for acknowledgement",
            {"ladder_step": step, "role": role},
        )
        step += 1
        runner.save_vars(incident_id, ladder_step=step)


def start(incident_id: str, roles: list[str], message: str, interval: float | None = None, start_at: int = 1) -> None:
    """Step 0 (the first role) is alerted by the playbook itself; the ladder continues from start_at."""
    if acknowledged(incident_id) or not roles:
        return
    interval = interval if interval is not None else settings.escalation_interval_seconds
    task = asyncio.create_task(_ladder(incident_id, roles, interval, message, settings.escalation_max_rounds, start_at))
    _tasks[incident_id] = task
    task.add_done_callback(lambda _: _tasks.pop(incident_id, None))


def cancel_all() -> None:
    for t in list(_tasks.values()):
        t.cancel()
    _tasks.clear()
