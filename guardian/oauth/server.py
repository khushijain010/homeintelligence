"""OAuth 2.1 authorization server for the Alexa+ MCP add-on.

Authorization code + PKCE (S256 only), `resource` required (RFC 8707) and bound into the
token `aud`, static client, exact redirect-URI allowlist, refresh-token rotation with reuse
detection. No dynamic client registration, no OIDC (Alexa+ supports neither).
Tokens use wall-clock time, never the demo clock.
"""

import base64
import hashlib
import hmac
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlencode

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc import jwt
from joserfc.errors import JoseError
from sqlmodel import select

from guardian import db
from guardian.config import settings
from guardian.models import OAuthCode, OAuthRefreshToken, new_id
from guardian.oauth import keys, owner

router = APIRouter()

SCOPES = ["guardian.read", "guardian.act"]
ACCESS_TTL = 3600
CODE_TTL = timedelta(minutes=10)
REFRESH_TTL = timedelta(days=30)

# claims of the token on the current MCP request (read by the tools)
current_claims: ContextVar[dict | None] = ContextVar("guardian_mcp_claims", default=None)


def _issuer() -> str:
    return settings.public_base_url.rstrip("/")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# --------------------------------------------------------------------------- metadata


def protected_resource_metadata() -> dict:
    return {
        "resource": settings.mcp_resource,
        "authorization_servers": [_issuer()],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_name": "Guardian",
    }


@router.get("/.well-known/oauth-protected-resource")
def prm() -> dict:
    return protected_resource_metadata()


@router.get("/.well-known/oauth-protected-resource/mcp")
def prm_for_mcp_path() -> dict:  # RFC 9728 path-suffixed form for the /mcp resource
    return protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata() -> dict:
    iss = _issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "jwks_uri": f"{iss}/oauth/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": SCOPES,
    }


@router.get("/oauth/jwks.json")
def jwks() -> dict:
    return {"keys": [keys.public_jwk()]}


# --------------------------------------------------------------------------- authorize


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    resp = owner.page("Guardian authorization", f'<h1 class="font-semibold mb-2">Cannot authorize</h1><p class="text-sm">{escape(message)}</p>')
    resp.status_code = status
    return resp


def _redirect_error(redirect_uri: str, state: str | None, error: str, description: str) -> RedirectResponse:
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=303)


def _validate_authorize(p: dict) -> tuple[HTMLResponse | RedirectResponse | None, str]:
    """Returns (error response or None, normalized scope)."""
    if p.get("client_id") != settings.alexa_oauth_client_id:
        return _error_page("Unknown client."), ""
    redirect_uri = p.get("redirect_uri", "")
    if redirect_uri not in settings.redirect_uris:
        return _error_page("Redirect URI is not on the allowlist."), ""
    state = p.get("state")
    if p.get("response_type") != "code":
        return _redirect_error(redirect_uri, state, "unsupported_response_type", "only code is supported"), ""
    if not p.get("code_challenge") or p.get("code_challenge_method") != "S256":
        return _redirect_error(redirect_uri, state, "invalid_request", "PKCE with S256 is required"), ""
    if p.get("resource") != settings.mcp_resource:
        return _redirect_error(redirect_uri, state, "invalid_target", "resource must be the MCP server URI"), ""
    requested = (p.get("scope") or "guardian.read").split()
    if not set(requested) <= set(SCOPES):
        return _redirect_error(redirect_uri, state, "invalid_scope", "unknown scope"), ""
    if not (settings.owner_email and settings.owner_password_hash):
        return _error_page("Owner login is not configured (run make set-password).", 503), ""
    return None, " ".join(s for s in SCOPES if s in requested)


AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge", "code_challenge_method", "resource")


def _consent_page(p: dict, scope: str, logged_in: bool, error: str = "") -> HTMLResponse:
    hidden = "".join(f'<input type="hidden" name="{k}" value="{escape(p.get(k) or "")}">' for k in AUTH_PARAMS)
    creds = (
        ""
        if logged_in
        else """<input name="email" type="email" required placeholder="Owner email" class="w-full border rounded px-3 py-2">
<input name="password" type="password" required placeholder="Password" class="w-full border rounded px-3 py-2">"""
    )
    labels = {"guardian.read": "Read home status, incidents, reports and care visits", "guardian.act": "Change home mode and approve or reject actions (Guardian's policy still decides)"}
    items = "".join(f"<li>{escape(labels[s])}</li>" for s in scope.split())
    msg = f'<p class="text-sm text-red-700">{escape(error)}</p>' if error else ""
    return owner.page(
        "Link Guardian",
        f"""<h1 class="text-lg font-semibold">Link Guardian to Alexa</h1>
<p class="text-sm text-slate-600 mt-1">Alexa is asking to:</p><ul class="list-disc ml-5 text-sm my-2">{items}</ul>{msg}
<form method="post" action="/oauth/authorize" class="space-y-3">{hidden}{creds}
<div class="flex gap-2"><button name="decision" value="allow" class="flex-1 bg-slate-900 text-white rounded py-2">Allow</button>
<button name="decision" value="deny" class="flex-1 border rounded py-2">Deny</button></div></form>""",
    )


@router.get("/oauth/authorize")
def authorize_get(request: Request):
    p = dict(request.query_params)
    err, scope = _validate_authorize(p)
    if err:
        return err
    return _consent_page(p, scope, logged_in=bool(request.session.get("user")))


@router.post("/oauth/authorize")
async def authorize_post(request: Request):
    form = await request.form()
    p = {k: str(v) for k, v in form.items()}
    err, scope = _validate_authorize(p)
    if err:
        return err
    if p.get("decision") != "allow":
        return _redirect_error(p["redirect_uri"], p.get("state"), "access_denied", "the owner denied access")
    if not request.session.get("user"):
        if not owner.check_owner(p.get("email", ""), p.get("password", "")):
            return _consent_page(p, scope, logged_in=False, error="Wrong email or password.")
        request.session["user"] = settings.owner_email
    code = secrets.token_urlsafe(32)
    with db.session() as s:
        s.add(
            OAuthCode(
                code_hash=_hash(code),
                client_id=p["client_id"],
                redirect_uri=p["redirect_uri"],
                scope=scope,
                code_challenge=p["code_challenge"],
                resource=p["resource"],
                subject=request.session["user"],
                expires_at=_utcnow() + CODE_TTL,
            )
        )
        s.commit()
    params = {"code": code}
    if p.get("state"):
        params["state"] = p["state"]
    return RedirectResponse(f"{p['redirect_uri']}?{urlencode(params)}", status_code=303)


# --------------------------------------------------------------------------- token


def _token_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status, headers={"Cache-Control": "no-store"})


def _client_ok(request: Request, form: dict) -> bool:
    client_id, secret = form.get("client_id"), form.get("client_secret")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            client_id, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return False
    if client_id != settings.alexa_oauth_client_id:
        return False
    if settings.alexa_oauth_client_secret:
        return hmac.compare_digest((secret or "").encode(), settings.alexa_oauth_client_secret.encode())
    return True  # public client: PKCE is the proof


def issue_access_token(subject: str, scope: str, client_id: str) -> str:
    now = int(time.time())
    claims = {
        "iss": _issuer(),
        "sub": subject,
        "aud": settings.mcp_resource,
        "scope": scope,
        "client_id": client_id,
        "iat": now,
        "exp": now + ACCESS_TTL,
        "jti": new_id(),
    }
    return jwt.encode({"alg": "RS256", "kid": "guardian-1"}, claims, keys.signing_key())


def _token_response(subject: str, scope: str, client_id: str, resource: str, family: str | None = None) -> JSONResponse:
    refresh = secrets.token_urlsafe(32)
    with db.session() as s:
        s.add(
            OAuthRefreshToken(
                token_hash=_hash(refresh),
                family_id=family or new_id(),
                client_id=client_id,
                scope=scope,
                resource=resource,
                subject=subject,
                expires_at=_utcnow() + REFRESH_TTL,
            )
        )
        s.commit()
    return JSONResponse(
        {
            "access_token": issue_access_token(subject, scope, client_id),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": scope,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/oauth/token")
async def token(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not _client_ok(request, form):
        return _token_error("invalid_client", "client authentication failed", 401)
    grant = form.get("grant_type")
    if grant == "authorization_code":
        return _code_grant(form)
    if grant == "refresh_token":
        return _refresh_grant(form)
    return _token_error("unsupported_grant_type", "use authorization_code or refresh_token")


def _code_grant(form: dict) -> JSONResponse:
    code, verifier, resource = form.get("code", ""), form.get("code_verifier", ""), form.get("resource", "")
    if not verifier:
        return _token_error("invalid_request", "code_verifier is required (PKCE)")
    if resource != settings.mcp_resource:
        return _token_error("invalid_target", "resource must be the MCP server URI")
    with db.session() as s:
        row = s.get(OAuthCode, _hash(code))
        if row is None or row.used or _aware(row.expires_at) < _utcnow():
            return _token_error("invalid_grant", "code is invalid, used or expired")
        row.used = True  # one-time, even if the checks below fail
        s.add(row)
        s.commit()
    if row.client_id != form.get("client_id", row.client_id) or row.redirect_uri != form.get("redirect_uri"):
        return _token_error("invalid_grant", "client or redirect_uri mismatch")
    if row.resource != resource:
        return _token_error("invalid_target", "resource differs from the authorization request")
    if not hmac.compare_digest(create_s256_code_challenge(verifier), row.code_challenge):
        return _token_error("invalid_grant", "PKCE verification failed")
    return _token_response(row.subject, row.scope, row.client_id, row.resource)


def _refresh_grant(form: dict) -> JSONResponse:
    resource = form.get("resource")
    if resource is not None and resource != settings.mcp_resource:
        return _token_error("invalid_target", "resource must be the MCP server URI")
    with db.session() as s:
        row = s.get(OAuthRefreshToken, _hash(form.get("refresh_token", "")))
        if row is None or row.revoked or _aware(row.expires_at) < _utcnow():
            return _token_error("invalid_grant", "refresh token is invalid or expired")
        if row.used:  # reuse of a rotated token: assume theft, revoke the family
            for t in s.exec(select(OAuthRefreshToken).where(OAuthRefreshToken.family_id == row.family_id)).all():
                t.revoked = True
                s.add(t)
            s.commit()
            return _token_error("invalid_grant", "refresh token was already used")
        scope = row.scope
        if form.get("scope"):
            if not set(form["scope"].split()) <= set(row.scope.split()):
                return _token_error("invalid_scope", "cannot widen scope on refresh")
            scope = " ".join(s_ for s_ in SCOPES if s_ in form["scope"].split())
        row.used = True
        s.add(row)
        s.commit()
    return _token_response(row.subject, scope, row.client_id, row.resource, family=row.family_id)


# --------------------------------------------------------------------------- resource server


def verify_bearer(authorization: str | None) -> tuple[int, dict | None]:
    """(200, claims) | (401, None) for missing/invalid/expired | (403, None) for wrong audience."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return 401, None
    raw = authorization[7:].strip()
    if settings.dev_mcp_token and not settings.is_production and hmac.compare_digest(raw.encode(), settings.dev_mcp_token.encode()):
        return 200, {"sub": "dev-token", "scope": " ".join(SCOPES)}
    try:
        claims = jwt.decode(raw, keys.signing_key(), algorithms=["RS256"]).claims
    except (JoseError, ValueError):
        return 401, None
    if claims.get("iss") != _issuer() or int(claims.get("exp", 0)) < time.time():
        return 401, None
    aud = claims.get("aud")
    if settings.mcp_resource not in (aud if isinstance(aud, list) else [aud]):
        return 403, None
    return 200, claims


class MCPAuthMiddleware:
    """Bearer check in front of the MCP endpoint. Alexa+ requires the 401 to carry
    no WWW-Authenticate header, so the SDK's built-in auth (which adds one) isn't used."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        status, claims = verify_bearer((headers.get(b"authorization") or b"").decode("latin-1") or None)
        if status != 200:
            body = b'{"error":"invalid_token"}' if status == 401 else b'{"error":"insufficient_scope"}'
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        reset = current_claims.set(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            current_claims.reset(reset)


def has_scope(scope: str) -> bool:
    claims = current_claims.get() or {}
    return scope in (claims.get("scope") or "").split()


def caller() -> str:
    claims = current_claims.get() or {}
    return f"alexa:{claims.get('sub', 'unknown')}"
