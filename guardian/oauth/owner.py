"""Owner login (email + bcrypt hash from .env) and the session gate for the dashboard."""

import hmac
import json
import logging
import secrets
from html import escape
from urllib.parse import quote

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from guardian.config import settings

log = logging.getLogger("guardian.auth")
router = APIRouter()

# reachable without a dashboard session (each has its own protection or none needed)
PUBLIC_PREFIXES = ("/login", "/logout", "/webhooks/", "/mcp", "/oauth/", "/.well-known/", "/legal/", "/static/")


def session_secret() -> str:
    if settings.session_secret:
        return settings.session_secret
    log.warning("SESSION_SECRET not set; using a random key (sessions end on restart)")
    return secrets.token_hex(32)


def login_enabled() -> bool:
    """Development without a password hash runs open on 127.0.0.1; production always requires login."""
    return settings.is_production or bool(settings.owner_password_hash)


def check_owner(email: str, password: str) -> bool:
    if not (settings.owner_email and settings.owner_password_hash):
        return False
    email_ok = hmac.compare_digest(email.strip().lower().encode(), settings.owner_email.strip().lower().encode())
    try:
        pw_ok = bcrypt.checkpw(password.encode(), settings.owner_password_hash.encode())
    except ValueError:
        pw_ok = False
    return email_ok and pw_ok


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


class OwnerAuthMiddleware:
    """Pure ASGI gate (keeps SSE streaming intact). Must sit inside SessionMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not login_enabled():
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith(PUBLIC_PREFIXES) or (scope.get("session") or {}).get("user"):
            return await self.app(scope, receive, send)
        if path.startswith("/api/"):
            body = json.dumps({"detail": "login required"}).encode()
            headers = [(b"content-type", b"application/json")]
            await send({"type": "http.response.start", "status": 401, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        location = "/login?next=" + quote(path)
        await send({"type": "http.response.start", "status": 303, "headers": [(b"location", location.encode())]})
        await send({"type": "http.response.body", "body": b""})


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<script src="https://cdn.tailwindcss.com/3.4.17"></script></head>
<body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
<div class="bg-white rounded-xl shadow p-6 w-full max-w-sm">{body}</div></body></html>"""
    )


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


@router.get("/login")
def login_form(next: str = "/", error: str = "") -> HTMLResponse:
    msg = '<p class="text-sm text-red-700 mb-2">Wrong email or password.</p>' if error else ""
    return page(
        "Guardian login",
        f"""<h1 class="text-lg font-semibold mb-3">Guardian</h1>{msg}
<form method="post" action="/login" class="space-y-3">
<input type="hidden" name="next" value="{escape(next)}">
<input name="email" type="email" required placeholder="Owner email" class="w-full border rounded px-3 py-2">
<input name="password" type="password" required placeholder="Password" class="w-full border rounded px-3 py-2">
<button class="w-full bg-slate-900 text-white rounded py-2">Sign in</button></form>""",
    )


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if not check_owner(email, password):
        return RedirectResponse(f"/login?error=1&next={quote(_safe_next(next))}", status_code=303)
    request.session["user"] = settings.owner_email
    return RedirectResponse(_safe_next(next), status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def actor(request: Request) -> str:
    """Who is acting, for audit records."""
    return (request.session.get("user") if "session" in request.scope else None) or "dashboard"
