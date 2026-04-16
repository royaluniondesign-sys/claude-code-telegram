"""FastAPI webhook + dashboard API server.

Runs in the same process as the bot, sharing the event loop.
Serves the monitoring dashboard at / and API endpoints at /api/*.

The actual route handlers live in src/api/routers/:
  system.py    — /health, /api/system, /api/status, /api/logs, /api/stream/logs
  brains.py    — /api/brains, /api/cortex, /api/learnings, /api/chat, /api/router
  routines.py  — /api/routines/* and /api/routines/jobs/*
  conductor.py — /api/conductor/*, /api/proactive/*, /api/stream/orchestration
  tasks.py     — /api/tasks/*
  memory.py    — /api/memory, /api/rag/*, /api/claude/context
  squad.py     — /api/squad/*, /api/team
  webhooks.py  — /webhooks/*, /auth/instagram/*, /api/social/*
  misc.py      — /api/mcp, /api/usage, /api/sqlite/*, /api/rud-server,
                  /api/shell, /api/terminal, /api/dashboard-url,
                  /api/tools, /api/crons, /api/invoke
"""

import asyncio
import json
import os as _os_top
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config.settings import Settings
from ..events.bus import EventBus
from ..storage.database import DatabaseManager
from .routers import system as system_router_mod
from .routers import brains as brains_router_mod
from .routers import routines as routines_router_mod
from .routers import conductor as conductor_router_mod
from .routers import tasks as tasks_router_mod
from .routers import memory as memory_router_mod
from .routers import squad as squad_router_mod
from .routers import misc as misc_router_mod
from .routers.webhooks import make_webhooks_router

logger = structlog.get_logger()

_DASHBOARD_DIR = Path(__file__).parent.parent.parent / "dashboard"


def create_api_app(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    brain_router: Any = None,
    rate_monitor: Any = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="AURA Dashboard API",
        version="1.0.0",
        docs_url="/docs" if settings.development_mode else None,
        redoc_url=None,
    )

    # CORS — allow dashboard to call API from any local origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    # ── DASHBOARD AUTH MIDDLEWARE ────────────────────────────
    # Protects all routes (except /health) with a token.
    # Token accepted via: ?token=... query param OR cookie "aura_token"
    # Set DASHBOARD_TOKEN in .env. If unset, dashboard is open (localhost dev).
    _DASHBOARD_TOKEN = _os_top.environ.get("DASHBOARD_TOKEN", "")
    _OPEN_PATHS = {"/health", "/favicon.ico"}

    @app.middleware("http")
    async def dashboard_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not _DASHBOARD_TOKEN:
            return await call_next(request)

        path = request.url.path

        # Always allow health + favicon
        if path in _OPEN_PATHS:
            return await call_next(request)

        # Check cookie first, then query param, then Authorization header
        cookie_token = request.cookies.get("aura_token", "")
        query_token = request.query_params.get("token", "")
        header_token = request.headers.get("X-Dashboard-Token", "")
        provided = cookie_token or query_token or header_token

        if provided != _DASHBOARD_TOKEN:
            # Return login page for browser requests, 401 for API
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            # Serve the proper login page from dashboard/login.html
            login_file = _DASHBOARD_DIR / "login.html"
            if login_file.exists():
                return HTMLResponse(login_file.read_text(encoding="utf-8"), status_code=401)
            # Minimal fallback (login.html missing)
            return HTMLResponse(
                f'<html><body style="background:#000;color:#fff;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh">'
                f'<form method="get" action="{path}" style="display:flex;flex-direction:column;gap:12px;background:#111;padding:32px;border-radius:12px;border:1px solid #222">'
                f'<b style="color:#8b5cf6;font-size:18px">AURA</b>'
                f'<input name="token" type="password" placeholder="Token" autofocus style="padding:10px;background:#1e1e2e;border:1px solid #333;color:#fff;border-radius:8px;font-size:14px">'
                f'<button type="submit" style="padding:10px;background:#7c3aed;border:none;color:#fff;border-radius:8px;cursor:pointer;font-weight:600">Entrar</button>'
                f'</form></body></html>',
                status_code=401,
            )

        # Valid token via query param → set cookie and redirect clean URL
        if query_token and query_token == _DASHBOARD_TOKEN:
            from starlette.responses import RedirectResponse
            redirect_path = path
            if request.url.query:
                other_params = "&".join(
                    f"{k}={v}" for k, v in request.query_params.items() if k != "token"
                )
                redirect_path = f"{path}?{other_params}" if other_params else path
            response = RedirectResponse(url=redirect_path, status_code=302)
            response.set_cookie("aura_token", _DASHBOARD_TOKEN, max_age=86400 * 30, httponly=True, samesite="lax")
            return response

        return await call_next(request)

    # ── INCLUDE ROUTERS ──────────────────────────────────────

    # Stateless routers (no shared state needed)
    app.include_router(system_router_mod.router)
    app.include_router(routines_router_mod.router)
    app.include_router(tasks_router_mod.router)
    app.include_router(memory_router_mod.router)
    app.include_router(squad_router_mod.router)
    app.include_router(misc_router_mod.router)
    app.include_router(brains_router_mod.router)
    app.include_router(conductor_router_mod.router)

    # Webhooks router needs event_bus + settings + db_manager
    app.include_router(make_webhooks_router(event_bus, settings, db_manager))

    # ── STATIC DASHBOARD ─────────────────────────────────────

    if _DASHBOARD_DIR.exists():
        app.mount("/app", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")
        # Serve Anthropic fonts (copied from Termora project)
        _fonts_dir = _DASHBOARD_DIR / "fonts"
        if _fonts_dir.exists():
            app.mount("/fonts", StaticFiles(directory=str(_fonts_dir)), name="fonts")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(
            str(_DASHBOARD_DIR / "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    return app


async def run_api_server(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    brain_router: Any = None,
    rate_monitor: Any = None,
) -> None:
    """Run the FastAPI server using uvicorn."""
    import uvicorn

    from ..infra.tunnel import start_dashboard_tunnel, stop_dashboard_tunnel

    app = create_api_app(event_bus, settings, db_manager, brain_router=brain_router, rate_monitor=rate_monitor)
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=settings.api_server_port,
        log_level="info" if not settings.debug else "debug",
    )
    server = uvicorn.Server(config)

    # Start cloudflared tunnel for dashboard in background
    tunnel_task = await start_dashboard_tunnel(port=settings.api_server_port)
    logger.info("dashboard_tunnel_started", port=settings.api_server_port)

    try:
        await server.serve()
    finally:
        tunnel_task.cancel()
        try:
            await tunnel_task
        except asyncio.CancelledError:
            pass
        await stop_dashboard_tunnel()
        logger.info("dashboard_tunnel_stopped")
