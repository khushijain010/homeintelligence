"""In-process wiring: ring-sandbox emulator (pytest plugin `ring_app`) + Guardian app, both
over ASGI transports. No sockets, no internet."""

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest
from ring_sandbox import webhooks as ring_webhooks

from guardian import clock, db
from guardian.ai import bedrock
from guardian.config import settings
from guardian.demo import director
from guardian.devices import virtual
from guardian.engine import correlator, incidents, runner
from guardian.main import create_app
from guardian.ring import client as ring_client_mod


@pytest.fixture(autouse=True)
def no_bedrock():
    """Tests never touch real AWS; individual tests inject a fake model."""
    bedrock.set_client(None)
    yield
    bedrock.set_client(None)


@pytest.fixture
async def guardian(ring_app, monkeypatch):
    monkeypatch.setattr(settings, "corroboration_seconds", 1.5)
    monkeypatch.setattr(settings, "valve_close_seconds", 0.2)
    monkeypatch.setattr(settings, "verify_quiet_seconds", 0.6)
    monkeypatch.setattr(settings, "approval_timeout_seconds", 5)
    monkeypatch.setattr(director, "LEAK_GAP_SECONDS", 0.2)
    monkeypatch.setattr(director, "ENTRY_GAP_SECONDS", 0.1)
    monkeypatch.setattr(director, "SEED_HISTORY", False)
    db.init_db("sqlite://")

    emu = httpx.ASGITransport(app=ring_app)
    ring_client_mod.set_client(ring_client_mod.RingClient("sandbox-token", "http://sandbox", transport=emu))
    director.set_control(httpx.AsyncClient(transport=emu, base_url="http://sandbox"))
    monkeypatch.setattr(settings, "dev_mcp_token", "test-mcp-token")
    app = create_app(with_lifespan=False)
    stop_mcp = await start_mcp(app)
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")

    async def post_webhook(payload: dict) -> None:
        body = ring_webhooks.encode(payload)
        sig = ring_webhooks.sign(settings.ring_webhook_secret, body)
        resp = await api.post("/webhooks/ring", content=body, headers={"X-Signature": sig})
        assert resp.status_code == 200, resp.text

    director.post_webhook = post_webhook
    task = asyncio.create_task(correlator.run_forever())
    await director.reset()
    clock.freeze(datetime.now(UTC))  # fixed clock for the rest of the test
    yield api
    task.cancel()
    runner.cancel_all()
    virtual.forget_all()
    clock.reset()
    director.post_webhook = None
    director.set_control(None)
    ring_client_mod.set_client(None)
    await api.aclose()
    await stop_mcp()


async def start_mcp(app):
    """Run the MCP session manager in its own task (anyio needs enter and exit in one task)."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def hold():
        async with app.state.mcp_session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(hold())
    await started.wait()

    async def stopper():
        stop.set()
        await task

    return stopper


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-11-25",
}


async def mcp_call(client, method: str, params: dict | None = None, token: str = "test-mcp-token", msg_id: int = 1):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"} if token else dict(MCP_HEADERS)
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    return await client.post("/mcp", json=body, headers=headers)


async def call_tool(client, name: str, arguments: dict | None = None, token: str = "test-mcp-token") -> dict:
    resp = await mcp_call(client, "tools/call", {"name": name, "arguments": arguments or {}}, token=token)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


async def wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def wait_terminal(playbook_id: str, timeout: float = 10.0):
    def done():
        rows = [i for i in all_incidents() if i.playbook_id == playbook_id]
        return rows[0] if rows and rows[0].status in incidents.TERMINAL and rows[0].report_text else None

    return await wait_for(done, timeout)


def all_incidents():
    from sqlmodel import select

    from guardian.models import Incident

    with db.session() as s:
        return list(s.exec(select(Incident)).all())
