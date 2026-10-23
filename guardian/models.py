from datetime import datetime
from typing import Any

from sqlmodel import JSON, Column, Field, SQLModel
from ulid import ULID

from guardian import clock


def new_id() -> str:
    return str(ULID())


def _json(default=dict) -> Any:
    return Field(default_factory=default, sa_column=Column(JSON))


class Home(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    timezone: str = "Asia/Kolkata"
    mode: str = "home"  # home | away | night


class Device(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    source: str  # ring | virtual
    external_id: str = Field(index=True)
    kind: str  # doorbell | camera | flood_sensor | contact_sensor | valve
    name: str
    room: str = ""
    online: bool = True
    battery_pct: int | None = None
    last_reported_at: datetime | None = None
    state: dict = _json()
    is_actuator: bool = False


class Signal(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    device_id: str = Field(index=True)
    type: str = Field(index=True)
    sub_type: str | None = None
    value: dict = _json()
    observed_at: datetime = Field(default_factory=clock.now)
    source_request_id: str | None = Field(default=None, unique=True)
    raw: dict = _json()


class Resident(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    role: str  # owner | resident_elder | caregiver | family


class Incident(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    playbook_id: str
    status: str = "open"
    severity: str = "warning"  # info | warning | critical
    confidence: float = 0.6
    title: str
    current_step: str = ""
    opened_at: datetime = Field(default_factory=clock.now)
    closed_at: datetime | None = None
    report_text: str | None = None
    # runner progress, persisted so a restart resumes the incident (Module 8)
    step_index: int | None = 0
    run_vars: dict = _json()
    step_deadline: datetime | None = None


class Evidence(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    incident_id: str = Field(index=True)
    kind: str  # signal | home_state | device_health | snapshot | policy_decision | human_decision | verification
    summary: str
    payload: dict = _json()
    created_at: datetime = Field(default_factory=clock.now)


class Action(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    incident_id: str = Field(index=True)
    action_type: str
    target_device_id: str | None = None
    policy_outcome: str  # allow | needs_approval | deny
    policy_reasons: list = _json(list)
    approval_status: str = "n/a"  # n/a | pending | approved | rejected | expired
    result: str | None = None  # ok | failed | timeout
    verified: str = "pending"  # pending | yes | no
    created_at: datetime = Field(default_factory=clock.now)
    decided_by: str | None = None  # who approved/rejected (owner email, alexa:<sub>)
    decided_at: datetime | None = None
    # "{incident_id}:{step}:{action}:{n}"; a re-run step finds its earlier action instead of acting twice
    idempotency_key: str | None = Field(default=None, index=True)


class IncidentReport(SQLModel, table=True):
    """LLM-written report that passed the grounding validator (or the reason it fell back)."""

    id: str = Field(default_factory=new_id, primary_key=True)
    incident_id: str = Field(index=True)
    source: str  # bedrock | template
    model_id: str | None = None
    title: str = ""
    summary: str = ""
    sentences: list = _json(list)  # [{"text", "cites": ["E1"]}]
    open_questions: list = _json(list)
    refs: dict = _json()  # "E1" -> {"type": "evidence"|"action"|"incident", "id": row id}
    errors: list = _json(list)  # validator errors from rejected attempts
    created_at: datetime = Field(default_factory=clock.now)


class PolicyOverride(SQLModel, table=True):
    """Owner-chosen tier for an editable policy rule (Module 7)."""

    action_type: str = Field(primary_key=True)
    tier: str  # auto | ask | never
    set_by: str = "owner"
    set_at: datetime = Field(default_factory=clock.now)


class LedgerEntry(SQLModel, table=True):
    """Append-only, hash-chained audit record (Module 5). Never updated or deleted."""

    seq: int = Field(primary_key=True)
    incident_id: str | None = Field(default=None, index=True)
    kind: str
    body: dict = _json()
    prev_hash: str
    hash: str
    created_at: datetime = Field(default_factory=clock.now)


class OAuthCode(SQLModel, table=True):
    """One-time authorization code (stored hashed)."""

    code_hash: str = Field(primary_key=True)
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str
    resource: str
    subject: str
    expires_at: datetime
    used: bool = False


class OAuthRefreshToken(SQLModel, table=True):
    """Refresh token (stored hashed). Rotated on every use; reuse revokes the whole family."""

    token_hash: str = Field(primary_key=True)
    family_id: str = Field(index=True)
    client_id: str
    scope: str
    resource: str
    subject: str
    expires_at: datetime
    used: bool = False
    revoked: bool = False


class CareSchedule(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    caregiver_id: str
    weekdays: list = _json(list)  # 0 = Monday
    start_local: str  # "10:00"
    end_local: str
    min_duration_min: int


class CareVisit(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    schedule_id: str
    date: str  # local date, ISO
    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    duration_min: int | None = None
    status: str  # completed | late | short | no_show | unverified
    camera_status: str | None = None  # what the front-door devices alone concluded
    resident_answer: str | None = None  # yes | no, from "Did {caregiver} come today?"


class RoutineBaseline(SQLModel, table=True):
    """Kamla's learned routine; recomputed nightly at 03:00 local."""

    id: str = Field(default_factory=new_id, primary_key=True)
    computed_at: datetime = Field(default_factory=clock.now)
    learned_days: int = 0
    slots: dict = _json()  # weekday "0".."6" -> 48 p_activity values (30-min slots)
    first_activity_p90_min: int | None = None  # minutes after local midnight
    longest_outing_min: int | None = None
    daily_counts: dict = _json()  # local date -> activity count


class MonitoringPause(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    started_at: datetime = Field(default_factory=clock.now)
    until: datetime
    set_by: str = "resident"
    ended_early: bool = False


class CheckIn(SQLModel, table=True):
    """A question for the resident on the (simulated Alexa+) Voice Console."""

    id: str = Field(default_factory=new_id, primary_key=True)
    kind: str  # wellbeing | care_confirm | announcement
    prompt_en: str
    prompt_hi: str
    incident_id: str | None = None
    visit_id: str | None = None
    status: str = "pending"  # pending | answered | expired
    answer: str | None = None  # ok | needs_help | no_response | yes | no | acknowledged
    transcript: str | None = None
    created_at: datetime = Field(default_factory=clock.now)
    answered_at: datetime | None = None
