"""AURA Dashboard — legacy shim (port 3000 → port 8080 redirect).

The canonical dashboard lives in src/api/server.py (port 8080).
This module exists only for backward compatibility: any request to port 3000
is immediately redirected to the real dashboard on port 8080.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

logger = structlog.get_logger()

# Module-level references to live instances (set by main.py — kept for API compat)
_brain_router: Optional[Any] = None
_rate_monitor: Optional[Any] = None


def set_deps(brain_router: Any, rate_monitor: Any) -> None:
    """Injected from main.py — kept for API compatibility, not used here."""
    global _brain_router, _rate_monitor
    _brain_router = brain_router
    _rate_monitor = rate_monitor


def _real_port() -> int:
    """Return the port of the real API/dashboard server."""
    return int(os.environ.get("API_SERVER_PORT", "8080"))


def create_dashboard_app() -> FastAPI:
    """Create a minimal redirect app."""
    app = FastAPI(title="AURA Dashboard Redirect", docs_url=None, redoc_url=None)

    @app.get("/{path:path}")
    async def redirect_all(request: Request, path: str) -> RedirectResponse:
        real = _real_port()
        target = f"http://localhost:{real}/{path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(url=target, status_code=302)

    return app


async def run_dashboard(host: str = "127.0.0.1", port: int = 3000) -> None:
    """Run the redirect shim (localhost only)."""
    import uvicorn

    real = _real_port()
    app = create_dashboard_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("dashboard_redirect_shim", from_port=port, to_port=real)
    await server.serve()
