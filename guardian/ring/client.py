"""Async Ring Partner API client (only the endpoints Guardian uses).

Shapes per docs/ring-api-notes.md. In dev/tests the base URL points at ring-sandbox.
"""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from guardian.config import settings


class RingClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 4,
    ):
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=10.0,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """429 → honour Retry-After, else exponential backoff starting at 1 s."""
        for attempt in range(self.max_retries + 1):
            resp = await self._http.request(method, path, **kwargs)
            if resp.status_code != 429 or attempt == self.max_retries:
                resp.raise_for_status()
                return resp
            try:
                delay = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                delay = 2.0**attempt
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def devices(self) -> list[dict[str, Any]]:
        """GET /v1/devices?include=status,capabilities → [{device, status, capabilities}]."""
        doc = (await self._request("GET", "/v1/devices", params={"include": "status,capabilities"})).json()
        included = {(r["type"], r["id"]): r for r in doc.get("included", [])}
        out = []
        for dev in doc.get("data", []):
            rels = dev.get("relationships", {})

            def side(name: str, rtype: str) -> dict:
                rid = ((rels.get(name) or {}).get("data") or {}).get("id")
                return included.get((rtype, rid), {}).get("attributes", {})

            out.append(
                {
                    "device": dev,
                    "status": side("status", "device-status"),
                    "capabilities": side("capabilities", "device-capabilities"),
                }
            )
        return out

    async def status(self, device_id: str) -> dict[str, Any]:
        doc = (await self._request("GET", f"/v1/devices/{device_id}/status")).json()
        return doc["data"]["attributes"]

    async def snapshot_latest(self, device_id: str, minutes: int = 10) -> dict[str, Any]:
        """Latest image in the last few minutes. Uses wall-clock time: the API rejects
        timestamps in the future, so the demo clock must not leak in here."""
        start = datetime.now(UTC) - timedelta(minutes=minutes)
        body = {
            "type": "latest_in_range",
            "start_timestamp": int(start.timestamp() * 1000),
            "image_options": {"format": "jpeg"},
        }
        resp = await self._request(
            "POST", f"/v1/devices/{device_id}/media/image/download", json=body, follow_redirects=True
        )
        ctype = resp.headers.get("Content-Type", "image/jpeg")
        return {
            "data_uri": f"data:{ctype};base64,{base64.b64encode(resp.content).decode()}",
            "timestamp": resp.headers.get("X-Media-Timestamp"),
        }

    async def chime_play(self, device_id: str, slot_event: str) -> None:
        """POST /v1/devices/{id}/media/audio/playback {"event": "ring-appstore-event-1"} → 204
        (Early Access; slots listed in the chime's configurations, see docs/ring-api-notes.md)."""
        await self._request("POST", f"/v1/devices/{device_id}/media/audio/playback", json={"event": slot_event})


_client: RingClient | None = None


def get_client() -> RingClient:
    global _client
    if _client is None:
        _client = RingClient(settings.ring_access_token, settings.ring_api_base_url)
    return _client


def set_client(client: RingClient | None) -> None:
    global _client
    _client = client
