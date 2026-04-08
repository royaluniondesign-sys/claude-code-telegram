"""AURA Dashboard — FastAPI app with API + frontend.

Serves the dashboard UI and provides JSON API endpoints
for brains, health, limits, costs, sessions, and system info.

Security:
  - Binds to 127.0.0.1 by default (localhost only).
  - Optional token auth via DASHBOARD_TOKEN env var.
  - CORS restricted to localhost origins.
"""

import asyncio
import os
import secrets
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

logger = structlog.get_logger()

# Module-level references to live instances (set by main.py)
_brain_router: Optional[Any] = None
_rate_monitor: Optional[Any] = None

# Auth token — set via env or auto-generated on first run
_dashboard_token: Optional[str] = os.environ.get("DASHBOARD_TOKEN")


def set_deps(brain_router: Any, rate_monitor: Any) -> None:
    """Inject live dependencies from main.py."""
    global _brain_router, _rate_monitor
    _brain_router = brain_router
    _rate_monitor = rate_monitor


def _generate_token() -> str:
    """Generate and persist a dashboard token if none set."""
    global _dashboard_token
    if _dashboard_token:
        return _dashboard_token
    token_file = Path.home() / ".aura" / "dashboard_token"
    if token_file.exists():
        _dashboard_token = token_file.read_text().strip()
    else:
        _dashboard_token = secrets.token_urlsafe(24)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(_dashboard_token)
        logger.info("dashboard_token_generated", path=str(token_file))
    return _dashboard_token


def create_dashboard_app() -> FastAPI:
    """Create the AURA Dashboard FastAPI app."""

    token = _generate_token()

    app = FastAPI(
        title="AURA Dashboard",
        version="0.10.0",
        docs_url="/api/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """Open access — dashboard runs on localhost only."""
        return await call_next(request)

    # ─── API Endpoints ───

    @app.get("/api/health")
    async def api_health() -> Dict[str, Any]:
        """Full system health check."""
        from ..infra.watchdog import Watchdog

        dog = Watchdog()
        report = await dog.check_all()
        return {
            "healthy": report.all_healthy,
            "services": [
                {
                    "name": s.name,
                    "emoji": s.emoji,
                    "running": s.is_running,
                    "details": s.details,
                    "error": s.error,
                }
                for s in report.services
            ],
            "disk_free_gb": report.disk_free_gb,
            "memory_used_pct": report.memory_used_pct,
            "warnings": report.warnings,
            "timestamp": report.timestamp,
        }

    @app.get("/api/brains")
    async def api_brains() -> Dict[str, Any]:
        """Brain status and info — uses live router from bot."""
        if _brain_router is None:
            return {"brains": [], "active": "unknown", "count": 0}

        infos = await _brain_router.get_all_info()
        return {
            "brains": infos,
            "active": _brain_router.active_brain_name,
            "count": len(_brain_router.available_brains),
        }

    @app.get("/api/limits")
    async def api_limits() -> Dict[str, Any]:
        """Rate limit usage for all brains — uses live monitor."""
        if _rate_monitor is None:
            from ..infra.rate_monitor import RateMonitor
            monitor = RateMonitor()
        else:
            monitor = _rate_monitor

        usage_list = monitor.get_all_usage()
        return {
            "brains": [
                {
                    "name": u.brain_name,
                    "requests": u.requests_in_window,
                    "errors": u.errors_in_window,
                    "limit": u.known_limit,
                    "usage_pct": u.usage_pct,
                    "window_remaining": u.window_remaining_str,
                    "is_rate_limited": u.is_rate_limited,
                }
                for u in usage_list
            ],
        }

    @app.get("/api/costs")
    async def api_costs() -> Dict[str, Any]:
        """Token economy stats."""
        from ..economy.cache import ResponseCache

        if _rate_monitor is None:
            from ..infra.rate_monitor import RateMonitor
            monitor = RateMonitor()
        else:
            monitor = _rate_monitor

        cache = ResponseCache()
        cache_stats = cache.stats()
        usage = {
            u.brain_name: {
                "requests": u.requests_in_window,
                "errors": u.errors_in_window,
            }
            for u in monitor.get_all_usage()
        }

        return {
            "cache": cache_stats,
            "usage_per_brain": usage,
        }

    @app.get("/api/context")
    async def api_context() -> Dict[str, Any]:
        """Latest Claude Code session context."""
        import json

        ctx_file = Path.home() / ".aura" / "context" / "latest.json"
        try:
            return json.loads(ctx_file.read_text())
        except Exception:
            return {"error": "No context available"}

    @app.get("/api/system")
    async def api_system() -> Dict[str, Any]:
        """System info — Mac specs, uptime, versions."""
        info: Dict[str, Any] = {}

        # Extended PATH for CLI detection
        extra_path = "/opt/homebrew/bin:/usr/local/bin"
        env_with_path = {**os.environ, "PATH": f"{extra_path}:{os.environ.get('PATH', '')}"}

        # Uptime
        try:
            proc = await asyncio.create_subprocess_shell(
                "uptime",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            info["uptime"] = stdout.decode().strip()
        except Exception:
            info["uptime"] = "unknown"

        # Mac chip
        try:
            proc = await asyncio.create_subprocess_shell(
                "/usr/sbin/sysctl -n machdep.cpu.brand_string",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            info["model"] = stdout.decode().strip() or "unknown"
        except Exception:
            info["model"] = "unknown"

        # Memory
        try:
            mem_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            info["total_ram_gb"] = round(mem_bytes / (1024 ** 3), 1)
        except Exception:
            info["total_ram_gb"] = 0

        # Disk
        try:
            usage = shutil.disk_usage(str(Path.home()))
            info["disk_total_gb"] = round(usage.total / (1024 ** 3), 1)
            info["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
        except Exception as e:
            logger.debug("disk_usage_error", error=str(e))

        # CLI versions (with extended PATH)
        for name, cmd in [
            ("claude", "claude --version"),
            ("codex", "codex --version"),
            ("gemini", "gemini --version"),
        ]:
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env_with_path,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                info[f"{name}_version"] = stdout.decode().strip()
            except Exception:
                info[f"{name}_version"] = "N/A"

        return info

    @app.get("/api/workflows")
    async def api_workflows() -> Dict[str, Any]:
        """Business workflow status and schedules."""
        from ..workflows.scheduler_setup import _WORKFLOW_DEFS

        workflows = []
        for wf in _WORKFLOW_DEFS:
            workflows.append({
                "name": wf["name"],
                "cron": wf["cron"],
                "description": wf["description"],
            })
        return {"workflows": workflows, "count": len(workflows)}

    @app.get("/api/logs")
    async def api_logs(lines: int = 50) -> Dict[str, Any]:
        """Recent bot logs."""
        log_file = Path.home() / "claude-code-telegram" / "logs" / "bot.stdout.log"
        try:
            all_lines = log_file.read_text().strip().split("\n")
            recent = all_lines[-min(lines, len(all_lines)) :]
            return {"lines": recent, "total": len(all_lines)}
        except Exception as e:
            return {"lines": [], "error": str(e)}

    @app.get("/api/terminal")
    async def api_terminal() -> Dict[str, Any]:
        """Termora terminal status and auth URL."""
        import urllib.request
        import json

        termora_port = 4030
        try:
            req = urllib.request.Request(
                f"http://localhost:{termora_port}/api/info"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                info = json.loads(resp.read())
            health_req = urllib.request.Request(
                f"http://localhost:{termora_port}/api/health"
            )
            with urllib.request.urlopen(health_req, timeout=3) as resp:
                health = json.loads(resp.read())
            return {
                "online": True,
                "tunnelUrl": info.get("tunnelUrl"),
                "authUrl": info.get("authUrl"),
                "tunnelMethod": info.get("tunnelMethod"),
                "machineName": info.get("machineName"),
                "health": health.get("status"),
            }
        except Exception as e:
            return {"online": False, "error": str(e)}

    # ─── Dashboard Frontend ───

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        """Serve the AURA Dashboard (no auth — localhost only)."""
        html_path = Path(__file__).parent / "index.html"
        return html_path.read_text()

    return app


async def run_dashboard(host: str = "127.0.0.1", port: int = 3000) -> None:
    """Run the dashboard server (localhost only by default)."""
    import uvicorn

    token = _generate_token()
    app = create_dashboard_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info(
        "AURA Dashboard started",
        url=f"http://localhost:{port}",
        token_hint=token[:6] + "...",
    )
    await server.serve()
