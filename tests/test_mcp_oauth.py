import base64
import hashlib
import statistics
import time
from urllib.parse import parse_qs, urlparse

import bcrypt
import httpx
import pytest
from joserfc import jwt

from guardian import db
from guardian.config import settings
from guardian.demo import director, seed
from guardian.devices import registry
from guardian.engine import incidents
from guardian.main import create_app
from guardian.models import Device
from guardian.oauth import keys

from .conftest import call_tool, mcp_call, start_mcp, wait_for, wait_terminal

BASE = "https://guardian.example"
REDIRECT = "https://alexa.example/callback"
PASSWORD = "correct horse battery"


@pytest.fixture
async def oauth_app(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(settings, "owner_email", "priya@example.com")
    monkeypatch.setattr(settings, "owner_password_hash", bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode())
    monkeypatch.setattr(settings, "alexa_oauth_client_id", "alexa-client")
    monkeypatch.setattr(settings, "alexa_oauth_client_secret", "s3cret")
    monkeypatch.setattr(settings, "alexa_oauth_redirect_uris", REDIRECT)
    monkeypatch.setattr(settings, "jwt_private_key_path", str(tmp_path / "jwt.pem"))
    monkeypatch.setattr(settings, "dev_mcp_token", "")
    (tmp_path / "jwt.pem").write_bytes(keys.generate_pem())
    keys.reset()
    db.init_db("sqlite://")
    seed.seed_db()
    app = create_app(with_lifespan=False)
    stop = await start_mcp(app)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    yield client
    await client.aclose()
    await stop()
    keys.reset()


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(b"v" * 40).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_params(challenge: str, **over) -> dict:
    p = {
        "response_type": "code",
        "client_id": "alexa-client",
        "redirect_uri": REDIRECT,
        "scope": "guardian.read guardian.act",
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{BASE}/mcp",
    }
    p.update(over)
    return {k: v for k, v in p.items() if v is not None}


async def get_code(client, challenge: str, **over) -> str:
    form = {**auth_params(challenge, **over), "decision": "allow", "email": "priya@example.com", "password": PASSWORD}
    resp = await client.post("/oauth/authorize", data=form)
    assert resp.status_code == 303, resp.text
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q["state"] == ["xyz"]
    return q["code"][0]


async def exchange(client, code: str, verifier: str, **over) -> httpx.Response:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
        "resource": f"{BASE}/mcp",
        **over,
    }
    form = {k: v for k, v in form.items() if v is not None}
    return await client.post("/oauth/token", data=form, auth=("alexa-client", "s3cret"))


async def login_tokens(client) -> dict:
    verifier, challenge = pkce()
    resp = await exchange(client, await get_code(client, challenge), verifier)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- discovery


async def test_metadata_documents(oauth_app):
    prm = (await oauth_app.get("/.well-known/oauth-protected-resource")).json()
    assert prm["resource"] == f"{BASE}/mcp"
    assert prm["scopes_supported"] == ["guardian.read", "guardian.act"]
    assert prm["authorization_servers"] == [BASE]
    asm = (await oauth_app.get("/.well-known/oauth-authorization-server")).json()
    assert "S256" in asm["code_challenge_methods_supported"]
    assert "registration_endpoint" not in asm  # Alexa+ doesn't use dynamic registration
    assert asm["token_endpoint"] == f"{BASE}/oauth/token"


async def test_unauthenticated_mcp_is_401_without_www_authenticate(oauth_app):
    resp = await mcp_call(oauth_app, "tools/list", token="")
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}
    resp = await mcp_call(oauth_app, "tools/list", token="not-a-jwt")
    assert resp.status_code == 401 and "www-authenticate" not in {k.lower() for k in resp.headers}


async def test_token_in_query_string_is_not_accepted(oauth_app):
    tok = (await login_tokens(oauth_app))["access_token"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    resp = await oauth_app.post(f"/mcp?access_token={tok}", json=body, headers={"Accept": "application/json, text/event-stream"})
    assert resp.status_code == 401


async def test_wrong_audience_is_403(oauth_app):
    now = int(time.time())
    claims = {"iss": BASE, "sub": "x", "aud": "https://other.example/mcp", "scope": "guardian.read", "iat": now, "exp": now + 60}
    bad = jwt.encode({"alg": "RS256", "kid": "guardian-1"}, claims, keys.signing_key())
    assert (await mcp_call(oauth_app, "tools/list", token=bad)).status_code == 403


async def test_expired_token_is_401(oauth_app):
    now = int(time.time())
    claims = {"iss": BASE, "sub": "x", "aud": f"{BASE}/mcp", "scope": "guardian.read", "iat": now - 7200, "exp": now - 3600}
    old = jwt.encode({"alg": "RS256", "kid": "guardian-1"}, claims, keys.signing_key())
    assert (await mcp_call(oauth_app, "tools/list", token=old)).status_code == 401


# --------------------------------------------------------------------------- authorize / token


async def test_authorize_requires_pkce_s256(oauth_app):
    _, challenge = pkce()
    for over in ({"code_challenge": None}, {"code_challenge_method": "plain"}):
        resp = await oauth_app.get("/oauth/authorize", params=auth_params(challenge, **over))
        assert resp.status_code == 303
        assert "error=invalid_request" in resp.headers["location"]


async def test_authorize_requires_resource(oauth_app):
    _, challenge = pkce()
    for over in ({"resource": None}, {"resource": "https://evil.example/mcp"}):
        resp = await oauth_app.get("/oauth/authorize", params=auth_params(challenge, **over))
        assert "error=invalid_target" in resp.headers["location"]


async def test_authorize_rejects_unlisted_redirect_and_client(oauth_app):
    _, challenge = pkce()
    resp = await oauth_app.get("/oauth/authorize", params=auth_params(challenge, redirect_uri="https://evil.example/cb"))
    assert resp.status_code == 400 and "location" not in resp.headers
    resp = await oauth_app.get("/oauth/authorize", params=auth_params(challenge, client_id="someone-else"))
    assert resp.status_code == 400


async def test_wrong_password_gets_no_code(oauth_app):
    _, challenge = pkce()
    form = {**auth_params(challenge), "decision": "allow", "email": "priya@example.com", "password": "nope"}
    resp = await oauth_app.post("/oauth/authorize", data=form)
    assert resp.status_code == 200 and "Wrong email or password" in resp.text


async def test_token_enforces_pkce_and_resource(oauth_app):
    verifier, challenge = pkce()
    resp = await exchange(oauth_app, await get_code(oauth_app, challenge), "wrong-verifier-" + "x" * 30)
    assert resp.json()["error"] == "invalid_grant"
    resp = await exchange(oauth_app, await get_code(oauth_app, challenge), verifier, resource=None)
    assert resp.json()["error"] == "invalid_target"
    resp = await exchange(oauth_app, await get_code(oauth_app, challenge), None)
    assert resp.json()["error"] == "invalid_request"


async def test_code_is_single_use_and_client_must_authenticate(oauth_app):
    verifier, challenge = pkce()
    code = await get_code(oauth_app, challenge)
    resp = await oauth_app.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": f"{BASE}/mcp"},
        auth=("alexa-client", "wrong"),
    )
    assert resp.status_code == 401
    assert (await exchange(oauth_app, code, verifier)).status_code == 200
    assert (await exchange(oauth_app, code, verifier)).json()["error"] == "invalid_grant"


async def test_access_token_shape(oauth_app):
    tokens = await login_tokens(oauth_app)
    claims = jwt.decode(tokens["access_token"], keys.signing_key()).claims
    assert claims["aud"] == f"{BASE}/mcp" and claims["sub"] == "priya@example.com"
    assert claims["exp"] - claims["iat"] == 3600
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "guardian.read guardian.act"


async def test_refresh_rotation_and_reuse_detection(oauth_app):
    first = await login_tokens(oauth_app)

    async def refresh(tok):
        return await oauth_app.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": tok, "resource": f"{BASE}/mcp"},
            auth=("alexa-client", "s3cret"),
        )

    second = await refresh(first["refresh_token"])
    assert second.status_code == 200 and second.json()["refresh_token"] != first["refresh_token"]
    assert (await refresh(first["refresh_token"])).json()["error"] == "invalid_grant"  # reused
    assert (await refresh(second.json()["refresh_token"])).json()["error"] == "invalid_grant"  # family revoked


# --------------------------------------------------------------------------- MCP tools over OAuth


async def test_mcp_initialize_and_list_tools_with_oauth_token(oauth_app):
    tok = (await login_tokens(oauth_app))["access_token"]
    init = await mcp_call(
        oauth_app,
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        token=tok,
    )
    assert init.status_code == 200, init.text
    assert init.json()["result"]["protocolVersion"] == "2025-11-25"
    tools = (await mcp_call(oauth_app, "tools/list", token=tok)).json()["result"]["tools"]
    assert {t["name"] for t in tools} >= {
        "get_home_status",
        "list_open_incidents",
        "get_incident_report",
        "approve_action",
        "set_home_mode",
        "get_care_visits",
    }


async def test_read_only_token_cannot_act(oauth_app):
    verifier, challenge = pkce()
    code = await get_code(oauth_app, challenge, scope="guardian.read")
    tok = (await exchange(oauth_app, code, verifier)).json()["access_token"]
    out = await call_tool(oauth_app, "set_home_mode", {"mode": "away"}, token=tok)
    assert out["data"]["ok"] is False
    out = await call_tool(oauth_app, "get_home_status", token=tok)
    assert out["data"]["mode"] == "home"


async def test_tool_speech_is_short(oauth_app):
    tok = (await login_tokens(oauth_app))["access_token"]
    for name in ("get_home_status", "list_open_incidents", "get_incident_report", "get_care_visits"):
        out = await call_tool(oauth_app, name, token=tok)
        assert 0 < len(out["speech"].split()) <= 60, (name, out["speech"])


async def test_tool_latency_p95_under_300ms(oauth_app):
    tok = (await login_tokens(oauth_app))["access_token"]
    timings = []
    for _ in range(200):
        t0 = time.perf_counter()
        await call_tool(oauth_app, "get_home_status", token=tok)
        timings.append(time.perf_counter() - t0)
    p95 = statistics.quantiles(timings, n=100)[94]
    assert p95 < 0.3, f"p95 {p95 * 1000:.0f} ms"


# --------------------------------------------------------------------------- policy cannot be bypassed


async def test_alexa_approval_still_goes_through_policy(guardian):
    """Owner approves by voice, but the valve went offline meanwhile: the runner's policy
    re-check denies, the valve is never commanded, and the incident escalates."""
    await director.run("leak_single_sensor")

    def pending():
        for inc in incidents.open_incidents("water_leak"):
            if any(a.approval_status == "pending" for a in incidents.actions(inc.id)):
                return inc
        return None

    inc = await wait_for(pending)
    valve = registry.all_devices("valve")[0]
    with db.session() as s:
        dev = s.get(Device, valve.id)
        dev.online = False
        s.add(dev)
        s.commit()

    out = await call_tool(guardian, "approve_action", {"decision": "approve"})
    assert out["data"]["ok"] is True and "re-check" in out["speech"]
    assert registry.get(valve.id).state.get("position") == "open"  # tool itself did nothing

    inc = await wait_terminal("water_leak")
    assert inc.status == "escalated"
    action = next(a for a in incidents.actions(inc.id) if a.action_type == "valve.close")
    assert action.approval_status == "approved" and action.decided_by == "alexa:dev-token"
    assert action.result is None  # never executed
    assert registry.get(valve.id).state.get("position") == "open"
    recheck = [e for e in incidents.evidence(inc.id) if "re-check" in e.summary]
    assert recheck and recheck[0].payload["outcome"] == "deny"


async def test_set_home_mode_and_status_tools(guardian):
    out = await call_tool(guardian, "set_home_mode", {"mode": "away"})
    assert out["data"]["mode"] == "away"
    status = await call_tool(guardian, "get_home_status")
    assert status["data"]["mode"] == "away" and "away mode" in status["speech"]


# --------------------------------------------------------------------------- owner login


async def test_dashboard_requires_login_when_password_set(oauth_app):
    assert (await oauth_app.get("/api/home")).status_code == 401
    resp = await oauth_app.get("/")
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    assert (await oauth_app.post("/webhooks/ring", content=b"{}")).status_code == 401  # HMAC, not login
    bad = await oauth_app.post("/login", data={"email": "priya@example.com", "password": "x"})
    assert "error=1" in bad.headers["location"]
    ok = await oauth_app.post("/login", data={"email": "Priya@Example.com", "password": PASSWORD, "next": "/"})
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert (await oauth_app.get("/api/home")).status_code == 200
    assert (await oauth_app.get("/legal/privacy")).status_code == 200


async def test_dev_token_disabled_in_production(oauth_app, monkeypatch):
    monkeypatch.setattr(settings, "dev_mcp_token", "dev-secret")
    assert (await mcp_call(oauth_app, "tools/list", token="dev-secret")).status_code == 200
    monkeypatch.setattr(settings, "env", "production")
    assert (await mcp_call(oauth_app, "tools/list", token="dev-secret")).status_code == 401


def test_addon_listing_limits():
    import json
    from pathlib import Path

    from PIL import Image

    root = Path(__file__).resolve().parents[1] / "alexa-addon"
    listing = json.loads((root / "listing.json").read_text(encoding="utf-8"))
    assert len(listing["name"]) <= 30
    assert len(listing["shortDescription"]) <= 123
    assert len(listing["fullDescription"]) <= 4000
    assert 3 <= len(listing["examplePhrases"]) <= 4
    assert all(len(p) <= 200 for p in listing["examplePhrases"])
    for size, path in listing["icons"].items():
        assert Image.open(root / path).size == (int(size), int(size))
    assert Image.open(root / listing["carouselImage"]).size == (600, 900)
