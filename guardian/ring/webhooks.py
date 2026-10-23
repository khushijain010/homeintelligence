"""POST /webhooks/ring: verify signature, dedupe on meta.request_id, normalize, store."""

import hashlib
import hmac
import json

from fastapi import APIRouter, Request, Response
from sqlmodel import select

from guardian import db
from guardian.config import settings
from guardian.devices import registry
from guardian.models import Signal
from guardian.ring import normalize

router = APIRouter()


def sign(secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def signature_ok(secret: str, raw_body: bytes, received: str | None) -> bool:
    if not received:
        return False
    return hmac.compare_digest(sign(secret, raw_body).encode(), received.strip().encode())


def _seen(request_id: str) -> bool:
    with db.session() as s:
        return s.exec(select(Signal.id).where(Signal.source_request_id == request_id)).first() is not None


def ingest_payload(payload: dict) -> None:
    """Normalize and store one (already verified) Ring webhook payload. Duplicates are ignored."""
    f = normalize.webhook_fields(payload)
    if _seen(f["request_id"]):
        return
    sig_type = normalize.webhook_signal_type(f["event_type"], f["sub_type"])
    device = registry.by_external_id(f["device_external_id"] or "") if f["source_type"] == "devices" else None
    if sig_type is None or device is None:
        return  # not something Guardian tracks
    registry.record_signal(
        device,
        sig_type,
        sub_type=f["sub_type"],
        observed_at=f["observed_at"],
        request_id=f["request_id"],
        raw=payload,
    )


@router.post("/webhooks/ring")
async def ring_webhook(request: Request) -> Response:
    raw = await request.body()
    if not signature_ok(settings.ring_webhook_secret, raw, request.headers.get("X-Signature")):
        return Response(status_code=401)
    try:
        ingest_payload(json.loads(raw))
    except (ValueError, KeyError, TypeError):
        return Response(status_code=400)
    return Response(status_code=200)
