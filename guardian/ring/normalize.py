"""Ring payloads → normalized Guardian signal types (names verified in docs/ring-api-notes.md)."""

from datetime import UTC, datetime
from typing import Any

WEBHOOK_TYPES = {
    "button_press": "doorbell.pressed",
    "device_online": "device.online",
    "device_offline": "device.offline",
    "flood_detected": "water.detected",
    "flood_cleared": "water.cleared",
    "contact_sensor_faulted": "contact.opened",
    "contact_sensor_cleared": "contact.closed",
    # Early Access (names verified in docs/ring-api-notes.md)
    "freeze_detected": "temperature.freeze",
    "freeze_cleared": "temperature.freeze_cleared",
    "temperature_exceeded": "temperature.exceeded",
    "temperature_cleared": "temperature.cleared",
    "tamper_detected": "device.tamper",
    "tamper_cleared": "device.tamper_cleared",
}


def webhook_signal_type(event_type: str, sub_type: str | None) -> str | None:
    """None means 'not a signal Guardian uses' (device_added, tamper_*, ...)."""
    if event_type == "motion_detected":
        return "motion.human" if sub_type == "human" else "motion.other"
    return WEBHOOK_TYPES.get(event_type)


def webhook_fields(payload: dict[str, Any]) -> dict[str, Any]:
    data, meta = payload["data"], payload["meta"]
    attrs = data.get("attributes", {})
    ts = attrs.get("timestamp")
    if ts is not None:
        observed = datetime.fromtimestamp(ts / 1000, tz=UTC)
    else:
        observed = datetime.fromisoformat(meta["time"].replace("Z", "+00:00"))
    return {
        "event_type": data["type"],
        "device_external_id": attrs.get("source"),
        "source_type": attrs.get("source_type"),
        "sub_type": attrs.get("sub_type"),
        "request_id": meta["request_id"],
        "observed_at": observed,
    }


def device_kind(device: dict, status: dict, capabilities: dict) -> str | None:
    """The Partner API has no kind field; infer it like ring-sandbox does."""
    if "flood_detection" in status:
        return "flood_sensor"
    if "contact_detection" in status:
        return "contact_sensor"
    image_url = (device.get("attributes", {}).get("image_url") or "").lower()
    video = capabilities.get("video") or {}
    if "doorbell" in image_url:
        return "doorbell"
    if video.get("codecs") or video.get("max_resolution"):
        return "camera"
    if "chime.play" in ((capabilities.get("audio") or {}).get("supported_actions") or []):
        return "chime"
    return None


def sensor_state(kind: str, status: dict) -> dict[str, Any]:
    if kind == "flood_sensor":
        return {"wet": bool((status.get("flood_detection") or {}).get("faulted"))}
    if kind == "contact_sensor":
        return {"open": bool((status.get("contact_detection") or {}).get("faulted"))}
    return {}


def battery_pct(status: dict) -> int | None:
    pct = (status.get("battery_status") or {}).get("percentage")
    return pct if isinstance(pct, int) and 0 <= pct <= 100 else None  # 255 = no reading


def reported_at(status: dict) -> datetime | None:
    raw = status.get("reported_at")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
