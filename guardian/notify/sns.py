"""SMS / email alerts through Amazon SNS (optional).

Published for critical incidents, approval requests and escalations, with a short summary
and a dashboard deep link. No SNS_TOPIC_ARN (or no AWS credentials) → dashboard only.
Per-role topics let the escalation ladder reach owner, family and neighbor separately.
"""

import logging
from typing import Protocol

from guardian.config import settings
from guardian.engine import incidents
from guardian.models import Action

log = logging.getLogger("guardian.sns")
SMS_LIMIT = 300


class Publisher(Protocol):
    def publish(self, TopicArn: str, Message: str, Subject: str) -> dict: ...


_client: Publisher | None = None
_checked = False


def get_client() -> Publisher | None:
    global _client, _checked
    if _checked:
        return _client
    _checked = True
    if not any(topic_for(r) for r in ("owner", "family", "neighbor")):
        return None
    try:
        import boto3

        if boto3.Session().get_credentials() is None:
            log.info("no AWS credentials; SNS alerts disabled")
            return None
        _client = boto3.client("sns", region_name=settings.aws_region)
    except Exception as exc:
        log.warning("SNS unavailable (%s); dashboard alerts only", exc)
        _client = None
    return _client


def set_client(client: Publisher | None) -> None:
    global _client, _checked
    _client, _checked = client, True


def topic_for(role: str) -> str:
    return getattr(settings, f"sns_topic_arn_{role}", "") or settings.sns_topic_arn


def deep_link(incident_id: str | None) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/#incident-{incident_id}" if incident_id else base


def publish(role: str, message: str, incident_id: str | None = None) -> str | None:
    """Returns the SNS message id, or None if SNS is off or the publish failed (never raises)."""
    client = get_client()
    topic = topic_for(role)
    if client is None or not topic:
        return None
    title = incidents.get(incident_id).title if incident_id and incidents.get(incident_id) else "Guardian"
    body = f"Guardian: {message}"[: SMS_LIMIT - 60] + f"\n{deep_link(incident_id)}"
    try:
        resp = client.publish(TopicArn=topic, Message=body, Subject=f"Guardian: {title}"[:100])
        return resp.get("MessageId")
    except Exception as exc:
        log.warning("SNS publish to %s failed: %s", role, exc)
        return None


def approval_needed(incident_id: str, action: Action) -> str | None:
    words = {"valve.close": "shut off the water", "plug.off": "turn off the heater plug", "chime.play": "sound the chime"}
    what = words.get(action.action_type, action.action_type)
    return publish("owner", f"Approval needed to {what}. Approve or reject in the dashboard (or ask Alexa).", incident_id)
