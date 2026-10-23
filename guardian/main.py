"""Guardian FastAPI app: API, webhook receiver, dashboard, Alexa+ MCP + OAuth, background tasks."""

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from guardian import db
from guardian.api import admin, legal, resident, routes, stream
from guardian.config import settings
from guardian.demo import seed
from guardian.devices import monitor, virtual
from guardian.engine import correlator, runner
from guardian.mcp_server import server as mcp_server
from guardian.oauth import owner
from guardian.oauth import server as oauth
from guardian.ring import poller, webhooks
from guardian.state import care_visits, routine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
STATIC = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if not seed.is_seeded():
        seed.seed_db()
    virtual.ensure_all()
    routine.routine_device()
    tasks = [
        asyncio.create_task(correlator.run_forever()),
        asyncio.create_task(poller.run_forever()),
        asyncio.create_task(care_visits.run_forever()),
        asyncio.create_task(routine.run_forever()),
        asyncio.create_task(monitor.run_forever()),
    ]
    runner.resume_all()  # continue open incidents from their saved step
    async with app.state.mcp_session_manager.run():
        yield
    for t in tasks:
        t.cancel()


def create_app(with_lifespan: bool = True) -> FastAPI:
    app = FastAPI(title="Guardian", lifespan=lifespan if with_lifespan else None)
    app.include_router(webhooks.router)
    app.include_router(routes.router)
    app.include_router(stream.router)
    app.include_router(owner.router)
    app.include_router(oauth.router)
    app.include_router(legal.router)
    app.include_router(resident.router)
    app.include_router(admin.router)

    # Alexa+ MCP endpoint, behind our own bearer check (401 without WWW-Authenticate)
    endpoint, manager = mcp_server.build_app()
    app.state.mcp_session_manager = manager
    app.router.routes.append(Route("/mcp", endpoint=oauth.MCPAuthMiddleware(endpoint)))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # order matters: SessionMiddleware (outer) must populate the session before the gate reads it
    app.add_middleware(owner.OwnerAuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=owner.session_secret(),
        same_site="lax",
        https_only=settings.is_production,
        session_cookie="guardian_session",
    )
    return app


app = create_app()
