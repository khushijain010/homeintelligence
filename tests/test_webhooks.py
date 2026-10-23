from ring_sandbox import webhooks as ring_webhooks
from sqlmodel import select

from guardian import db
from guardian.config import settings
from guardian.demo import seed
from guardian.models import Signal


def _payload(request_id: str = "req-1") -> dict:
    return ring_webhooks.build_event(
        event_type="button_press", device_id=seed.DEVICE_IDS["Front Door"], request_id=request_id
    )


def _signals() -> list[Signal]:
    with db.session() as s:
        return list(s.exec(select(Signal).where(Signal.type == "doorbell.pressed")).all())


async def _post(api, body: bytes, signature: str | None):
    headers = {"X-Signature": signature} if signature else {}
    return await api.post("/webhooks/ring", content=body, headers=headers)


async def test_valid_signature_accepted(guardian):
    body = ring_webhooks.encode(_payload())
    resp = await _post(guardian, body, ring_webhooks.sign(settings.ring_webhook_secret, body))
    assert resp.status_code == 200
    [sig] = _signals()
    assert sig.source_request_id == "req-1"


async def test_tampered_body_rejected(guardian):
    body = ring_webhooks.encode(_payload())
    signature = ring_webhooks.sign(settings.ring_webhook_secret, body)
    tampered = body.replace(b"button_press", b"motion_detected")
    assert (await _post(guardian, tampered, signature)).status_code == 401
    assert _signals() == []


async def test_wrong_key_rejected(guardian):
    body = ring_webhooks.encode(_payload())
    assert (await _post(guardian, body, ring_webhooks.sign("not-the-key", body))).status_code == 401


async def test_missing_signature_rejected(guardian):
    body = ring_webhooks.encode(_payload())
    assert (await _post(guardian, body, None)).status_code == 401


async def test_duplicate_delivery_ignored(guardian):
    body = ring_webhooks.encode(_payload("req-dup"))
    signature = ring_webhooks.sign(settings.ring_webhook_secret, body)
    assert (await _post(guardian, body, signature)).status_code == 200
    assert (await _post(guardian, body, signature)).status_code == 200
    assert len(_signals()) == 1


async def test_sensor_event_normalized(guardian):
    payload = ring_webhooks.build_event(event_type="flood_detected", device_id=seed.DEVICE_IDS["Kitchen Leak Sensor"])
    body = ring_webhooks.encode(payload)
    await _post(guardian, body, ring_webhooks.sign(settings.ring_webhook_secret, body))
    with db.session() as s:
        assert s.exec(select(Signal).where(Signal.type == "water.detected")).first() is not None
