"""FastAPI webhook + dashboard API server.

Runs in the same process as the bot, sharing the event loop.
Serves the monitoring dashboard at / and API endpoints at /api/*.
"""

import asyncio
import json
import os as _os_top
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import WebhookEvent
from ..storage.database import DatabaseManager
from .auth import verify_github_signature, verify_shared_secret

logger = structlog.get_logger()

_ANSI_RE = re.compile(r"\x1B\[[0-9;]*[mK]")
_DASHBOARD_DIR = Path(__file__).parent.parent.parent / "dashboard"


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


async def _run_squad_with_hooks(squad: Any, task: str) -> None:  # type: ignore[type-arg]
    """Run squad and fire post-completion hooks."""
    try:
        result = await squad.run(task, notify_fn=None)
    except Exception as exc:
        result = f"[error] {exc}"
    await _after_squad_complete(task, result or "")


async def _after_squad_complete(original_task: str, result: str) -> None:
    """After squad completes: propose next tasks + notify Telegram."""
    import os as _os
    import httpx as _httpx

    # 1. Generate next task suggestions via LLM (haiku — cheap)
    suggestions_created: list[str] = []
    try:
        from src.brains.router import BrainRouter as _BR
        from src.infra.task_store import create_task as _ct
        _router = _BR()
        suggestion_prompt = (
            f"Tarea completada: {original_task}\n\n"
            f"Resultado (resumen): {result[:600]}\n\n"
            "Sugiere exactamente 3 tareas de seguimiento concretas y accionables. "
            "Una por línea, formato estricto: TAREA: [título corto] | DESC: [descripción breve]\n"
            "Sé específico. Sin explicaciones extra."
        )
        suggestions_raw = await _router.call("haiku", suggestion_prompt, max_tokens=400)
        lines = [l.strip() for l in suggestions_raw.split("\n") if "TAREA:" in l]
        for line in lines[:3]:
            try:
                title = line.split("TAREA:")[1].split("|")[0].strip()[:120]
                desc = line.split("DESC:")[1].strip()[:300] if "DESC:" in line else ""
                _ct(
                    title=title,
                    description=desc,
                    category="content",
                    priority="medium",
                    created_by="squad_ai",
                    tags=["auto-suggested", "siguiente"],
                )
                suggestions_created.append(title)
            except Exception:
                pass
    except Exception:
        pass

    # 2. Notify Telegram with result + next steps
    token = _os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_raw = _os.environ.get("ALLOWED_USERS", "")
    if token and allowed_raw:
        chat_ids = [u.strip() for u in allowed_raw.split(",") if u.strip().isdigit()]
        summary = result[:700] + ("…" if len(result) > 700 else "")
        next_block = ""
        if suggestions_created:
            next_block = "\n\n📋 <b>Próximos pasos sugeridos:</b>\n" + "\n".join(
                f"  • {t}" for t in suggestions_created
            )
        msg = (
            f"✅ <b>Squad completó:</b> <i>{original_task[:80]}</i>\n\n"
            f"{summary}"
            f"{next_block}"
        )
        async with _httpx.AsyncClient(timeout=10) as _client:
            for cid in chat_ids[:3]:
                try:
                    await _client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": int(cid), "text": msg, "parse_mode": "HTML"},
                    )
                except Exception:
                    pass


# ── Instagram OAuth helpers (module-level, shared across requests) ────────────

_ig_oauth_state: Dict[str, str] = {}


def _update_env_instagram_token(token: str, user_id: str) -> None:
    """Update INSTAGRAM_ACCESS_TOKEN in .env file and runtime env."""
    import os as _os
    from pathlib import Path as _Path
    env_file = (_Path(__file__).parent.parent.parent / ".env").resolve()
    if not env_file.exists():
        return
    lines = env_file.read_text().splitlines()
    new_lines, token_written, uid_written = [], False, False
    for line in lines:
        if line.startswith("INSTAGRAM_ACCESS_TOKEN="):
            new_lines.append(f"INSTAGRAM_ACCESS_TOKEN={token}")
            token_written = True
        elif line.startswith("INSTAGRAM_ACCOUNT_ID=") and user_id:
            new_lines.append(f"INSTAGRAM_ACCOUNT_ID={user_id}")
            uid_written = True
        else:
            new_lines.append(line)
    if not token_written:
        new_lines.append(f"INSTAGRAM_ACCESS_TOKEN={token}")
    if not uid_written and user_id:
        new_lines.append(f"INSTAGRAM_ACCOUNT_ID={user_id}")
    env_file.write_text("\n".join(new_lines) + "\n")
    _os.environ["INSTAGRAM_ACCESS_TOKEN"] = token
    if user_id:
        _os.environ["INSTAGRAM_ACCOUNT_ID"] = user_id


async def _notify_ig_auth_success(user_id: str, expires_in: int) -> None:
    """Send Telegram message when Instagram OAuth completes."""
    import os as _os
    import aiohttp as _aio
    bot_token = _os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = (_os.environ.get("NOTIFICATION_CHAT_IDS", "") or "").split(",")[0].strip()
    if not bot_token or not chat_id:
        return
    days = expires_in // 86400
    msg = (
        f"✅ <b>Instagram OAuth completado!</b>\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Token válido por <b>{days} días</b> — auto-refresh activado."
    )
    async with _aio.ClientSession() as sess:
        await sess.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
        )


# ─────────────────────────────────────────────────────────────────────────────

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

    # ── HEALTH ──────────────────────────────────────────────

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    # ── SYSTEM METRICS ───────────────────────────────────────

    @app.get("/api/system")
    async def get_system_metrics() -> Dict[str, Any]:
        """Real-time system metrics: RAM, CPU, disk. Uses vm_stat on macOS."""
        import re as _re
        import shutil as _shutil
        import subprocess as _sp

        result: Dict[str, Any] = {"ok": True}

        # RAM via vm_stat (macOS accurate, no psutil needed)
        try:
            _pg = int(_sp.check_output(["/usr/sbin/sysctl", "-n", "hw.pagesize"], timeout=3).strip())
            _tb = int(_sp.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=3).strip())
            _vm = _sp.check_output("vm_stat", shell=True, timeout=3, text=True)

            def _pgs(pat: str) -> int:
                m = _re.search(pat, _vm)
                return int(m.group(1).rstrip(".")) if m else 0

            _avail = (
                _pgs(r"Pages free:\s+(\d+)")
                + _pgs(r"Pages speculative:\s+(\d+)")
                + _pgs(r"Pages purgeable:\s+(\d+)")
                + _pgs(r"Pages inactive:\s+(\d+)")
            ) * _pg
            _used = _tb - _avail
            result["ram"] = {
                "total_gb": round(_tb / 1e9, 1),
                "used_gb": round(_used / 1e9, 1),
                "free_gb": round(_avail / 1e9, 1),
                "pct": round(_used / _tb * 100, 1) if _tb > 0 else 0,
            }
        except Exception as _e:
            result["ram"] = {"error": str(_e)}

        # Disk
        try:
            du = _shutil.disk_usage("/")
            result["disk"] = {
                "total_gb": round(du.total / 1e9, 1),
                "used_gb": round(du.used / 1e9, 1),
                "free_gb": round(du.free / 1e9, 1),
                "pct": round(du.used / du.total * 100, 1),
            }
        except Exception as _e:
            result["disk"] = {"error": str(_e)}

        # CPU (load average — no psutil needed)
        try:
            import os as _os
            load = _os.getloadavg()
            result["cpu"] = {
                "load_1m": round(load[0], 2),
                "load_5m": round(load[1], 2),
                "load_15m": round(load[2], 2),
            }
        except Exception as _e:
            result["cpu"] = {"error": str(_e)}

        return result

    # ── STATUS ───────────────────────────────────────────────

    @app.get("/api/status")
    async def get_status() -> Dict[str, Any]:
        """AURA live status — system, brains, logs."""
        import asyncio
        import os
        import shutil
        from datetime import UTC, datetime

        result: Dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Bot process
        try:
            proc = await asyncio.create_subprocess_shell(
                "launchctl list com.aura.telegram-bot 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            text = out.decode()
            pid: Any = None
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 1 and parts[0].lstrip('"').rstrip('";').isdigit():
                    val = parts[0].strip('"').rstrip(";")
                    if val.isdigit():
                        pid = int(val)
                        break
            # Also try grep PID line
            for line in text.splitlines():
                if '"PID"' in line:
                    m = re.search(r"\d+", line)
                    if m:
                        pid = int(m.group())
                        break
            result["bot"] = {"running": pid is not None, "pid": pid}
            # Uptime via ps
            if pid:
                try:
                    import subprocess as _sp, time as _t
                    r2 = _sp.run(
                        ["ps", "-o", "lstart=", "-p", str(pid)],
                        capture_output=True, text=True, timeout=3,
                    )
                    if r2.stdout.strip():
                        from datetime import datetime as _dt
                        started = _dt.strptime(r2.stdout.strip(), "%c")
                        uptime_sec = int(_t.time() - started.timestamp())
                        result["uptime_sec"] = uptime_sec
                except Exception:
                    pass
        except Exception:
            result["bot"] = {"running": False, "pid": None}

        # Disk
        try:
            du = shutil.disk_usage("/")
            result["system"] = {
                "disk_pct": round(du.used / du.total * 100, 1),
                "disk_free_gb": round(du.free / 1e9, 1),
                "disk_total_gb": round(du.total / 1e9, 1),
            }
        except Exception:
            result["system"] = {}

        # RAM — macOS accurate: hw.pagesize (16384 on Apple Silicon) + count
        # free + speculative + purgeable + inactive pages as available.
        try:
            import re as _re
            import subprocess as _sp

            _sysctl = "/usr/sbin/sysctl"
            _pg = int(_sp.check_output([_sysctl, "-n", "hw.pagesize"], timeout=3).strip())
            _tb = int(_sp.check_output([_sysctl, "-n", "hw.memsize"],  timeout=3).strip())
            _vm = _sp.check_output("vm_stat", shell=True, timeout=3, text=True)

            def _pgs(pat: str) -> int:
                m = _re.search(pat, _vm)
                return int(m.group(1).rstrip(".")) if m else 0

            _avail = (
                _pgs(r"Pages free:\s+(\d+)")
                + _pgs(r"Pages speculative:\s+(\d+)")
                + _pgs(r"Pages purgeable:\s+(\d+)")
                + _pgs(r"Pages inactive:\s+(\d+)")
            ) * _pg
            if _tb > 0:
                result["system"]["ram_pct"]     = round((_tb - _avail) / _tb * 100, 1)
                result["system"]["ram_free_gb"] = round(_avail / 1e9, 1)
                result["system"]["ram_total_gb"] = round(_tb / 1e9, 1)
        except Exception as _ram_err:
            logger.warning("ram_stat_failed", error=str(_ram_err))

        # Brain rate limits
        try:
            from ..infra.rate_monitor import BRAIN_LIMITS, RateMonitor

            monitor = RateMonitor()
            brains = []
            for u in monitor.get_all_usage():
                limits = BRAIN_LIMITS.get(u.brain_name, {})
                pct = u.usage_pct
                warn_t = limits.get("warn_threshold", 0.75)
                brains.append(
                    {
                        "name": u.brain_name,
                        "tier": limits.get("tier", "?"),
                        "requests": u.requests_in_window,
                        "limit": u.known_limit,
                        "usage_pct": round(pct * 100, 1) if pct is not None else None,
                        "window": limits.get("window", "?"),
                        "resets_in": u.window_remaining_str,
                        "is_rate_limited": u.is_rate_limited,
                        "errors": u.errors_in_window,
                        "unlimited": u.known_limit is None,
                        "status": (
                            "rate_limited"
                            if u.is_rate_limited
                            else ("warn" if pct and pct >= warn_t else "ok")
                        ),
                    }
                )
            result["brains"] = brains
        except Exception as e:
            result["brains"] = []
            result["brains_error"] = str(e)

        # Log error count
        log_path = Path.home() / "claude-code-telegram/logs/bot.stdout.log"
        try:
            res = subprocess.run(
                ["grep", "-c", "error", str(log_path)],
                capture_output=True,
                text=True,
                timeout=3,
            )
            result["logs"] = {"error_count": int(res.stdout.strip() or "0")}
        except Exception:
            result["logs"] = {"error_count": 0}

        return result

    # ── LOGS ─────────────────────────────────────────────────

    @app.get("/api/logs")
    async def get_logs(n: int = 150, level: Optional[str] = None) -> Dict[str, Any]:
        """Return recent log entries from bot stdout log."""
        log_path = Path.home() / "claude-code-telegram/logs/bot.stdout.log"
        try:
            raw = log_path.read_text(errors="replace").splitlines()
            entries: List[Dict[str, Any]] = []
            for line in raw[-600:]:
                clean = _strip_ansi(line).strip()
                if not clean:
                    continue
                cl = clean.lower()
                if "error" in cl or '"level":"error"' in cl:
                    lvl = "error"
                elif "warning" in cl or "warn" in cl:
                    lvl = "warning"
                elif "debug" in cl:
                    lvl = "debug"
                else:
                    lvl = "info"
                if level and lvl != level:
                    continue
                entries.append({"text": clean, "level": lvl})
            return {"entries": entries[-n:], "total": len(raw)}
        except Exception as e:
            return {"entries": [], "error": str(e), "total": 0}

    # ── TOOLS ────────────────────────────────────────────────

    @app.get("/api/tools")
    async def get_tools() -> Dict[str, Any]:
        """List all registered AURA tools from the action registry."""
        try:
            from ..actions.registry import registry

            tools = []
            for name, spec in registry().items():
                tools.append(
                    {
                        "name": name,
                        "description": getattr(spec, "description", ""),
                        "category": getattr(spec, "category", "general"),
                        "cacheable": getattr(spec, "cacheable", False),
                    }
                )
            return {"tools": tools, "count": len(tools)}
        except Exception as e:
            return {"tools": [], "error": str(e), "count": 0}

    # ── CRON JOBS ────────────────────────────────────────────

    @app.get("/api/crons")
    async def get_crons() -> Dict[str, Any]:
        """List scheduled workflow definitions."""
        try:
            from ..workflows.scheduler_setup import _WORKFLOW_DEFS

            jobs = [
                {
                    "name": w["name"],
                    "cron": w["cron"],
                    "description": w.get("description", ""),
                    "module": w.get("module", ""),
                    "enabled": w.get("enabled", True),
                }
                for w in _WORKFLOW_DEFS
            ]
            return {"jobs": jobs}
        except Exception as e:
            return {"jobs": [], "error": str(e)}

    # ── MCP SERVERS ──────────────────────────────────────────

    @app.get("/api/mcp")
    async def get_mcp_status() -> Dict[str, Any]:
        """Return MCP server registration from settings.json."""
        settings_path = Path.home() / ".claude/settings.json"
        try:
            data = json.loads(settings_path.read_text())
            servers = data.get("mcpServers", {})
            result = [
                {
                    "name": name,
                    "command": cfg.get("command", ""),
                    "args": cfg.get("args", []),
                    "cwd": cfg.get("cwd", ""),
                    "enabled": True,
                }
                for name, cfg in servers.items()
            ]
            return {"servers": result}
        except Exception as e:
            return {"servers": [], "error": str(e)}

    # ── BRAINS LIVE STATUS ───────────────────────────────────

    @app.get("/api/brains")
    async def get_brains_status() -> Dict[str, Any]:
        """Real-time rate limit status for all brains with exact reset times."""
        import time as _time
        try:
            from ..infra.rate_monitor import BRAIN_LIMITS, RateMonitor
            monitor = RateMonitor()
            brains = []
            for u in monitor.get_all_usage():
                limits = BRAIN_LIMITS.get(u.brain_name, {})
                pct = u.usage_pct
                warn_t = limits.get("warn_threshold", 0.75)
                is_rl = u.is_rate_limited
                brains.append({
                    "name": u.brain_name,
                    "tier": limits.get("tier", "?"),
                    "requests": u.requests_in_window,
                    "limit": u.known_limit,
                    "usage_pct": round(pct * 100, 1) if pct is not None else None,
                    "window": limits.get("window", "?"),
                    "window_seconds": u.window_seconds,
                    "window_remaining_seconds": u.window_remaining_seconds,
                    "window_remaining_str": u.window_remaining_str,
                    "errors": u.errors_in_window,
                    "unlimited": u.known_limit is None,
                    "is_rate_limited": is_rl,
                    # Rate limit recovery info
                    "recover_at": u.recover_at,          # unix timestamp or null
                    "recover_in_seconds": u.recover_in_seconds if is_rl else 0,
                    "recover_in_str": u.recover_in_str if is_rl else None,
                    "rate_limited_at": u.rate_limited_at,
                    "status": (
                        "rate_limited" if is_rl
                        else ("warn" if pct and pct >= warn_t else "ok")
                    ),
                    "available": not is_rl,
                })
            # Pick the current best brain (first available in priority order)
            priority = ["haiku", "sonnet", "opus", "gemini", "codex", "opencode", "openrouter"]
            best = next((b["name"] for b in brains
                         if b["available"] and b["name"] in priority), None)
            return {
                "brains": brains,
                "best_available": best,
                "any_available": any(b["available"] for b in brains),
                "server_time": _time.time(),  # unix ts for client clock sync
            }
        except Exception as e:
            return {"brains": [], "error": str(e)}

    # ── CORTEX STATUS ────────────────────────────────────────

    @app.get("/api/cortex")
    async def get_cortex_status() -> Dict[str, Any]:
        """Return AURA Cortex learning state — scores, bypasses, session context."""
        cortex_path = Path.home() / ".aura" / "cortex.json"
        if not cortex_path.exists():
            return {
                "total_interactions": 0,
                "learned_rules": 0,
                "best_by_intent": {},
                "active_bypasses": [],
                "session_context": {},
                "last_updated": None,
                "note": "Cortex has no data yet — interact with the bot to start learning.",
            }
        try:
            raw = json.loads(cortex_path.read_text(encoding="utf-8"))
            brain_scores = raw.get("brain_scores", {})
            error_patterns = raw.get("error_patterns", [])

            # Best brain per intent (highest combined score)
            best_by_intent: Dict[str, Any] = {}
            for brain_name, intents in brain_scores.items():
                for intent_name, stats in intents.items():
                    score = stats.get("score", 0.0)
                    current = best_by_intent.get(intent_name)
                    if current is None or score > current.get("score", 0.0):
                        best_by_intent[intent_name] = {
                            "brain": brain_name,
                            "score": round(score, 3),
                            "samples": stats.get("samples", 0),
                            "avg_latency_ms": stats.get("avg_latency_ms", 0),
                        }

            bypasses = [
                {
                    "from": p.get("brain", ""),
                    "intent": p.get("intent", ""),
                    "to": p.get("bypass_to", "haiku"),
                    "failures": p.get("count", 0),
                    "note": p.get("note", ""),
                    "created": p.get("created", ""),
                }
                for p in error_patterns
            ]

            return {
                "total_interactions": raw.get("total_interactions", 0),
                "learned_rules": len(error_patterns),
                "best_by_intent": best_by_intent,
                "active_bypasses": bypasses,
                "session_context": raw.get("session_context", {}),
                "last_updated": raw.get("last_updated", ""),
            }
        except Exception as exc:
            logger.warning("cortex_api_error", error=str(exc))
            return {"error": str(exc), "total_interactions": 0}

    # ── TEAM ACTIVITY ────────────────────────────────────────

    @app.get("/api/team")
    async def get_team_activity() -> Dict[str, Any]:
        """Real-time squad activity snapshot."""
        try:
            from src.agents.activity import get_tracker
            return get_tracker().snapshot()
        except Exception as e:
            return {"run_active": False, "agents": {}, "messages": [], "error": str(e)}

    @app.post("/api/squad/stop")
    async def stop_squad(_: Request) -> Dict[str, Any]:
        """Request the running squad to stop."""
        try:
            from src.agents.activity import get_tracker
            tracker = get_tracker()
            if not tracker._run_active:
                return {"ok": False, "msg": "Sin tarea activa"}
            tracker.request_stop()
            return {"ok": True, "msg": "Stop solicitado — el squad finalizará tras la tarea actual"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/squad/run")
    async def run_squad_task(request: Request) -> Dict[str, Any]:
        """Trigger a squad run from the dashboard. Body: {task: str}"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        task = (body.get("task") or "").strip()
        if not task:
            raise HTTPException(status_code=400, detail="task required")
        try:
            from src.agents.squad import get_squad, AgentSquad
            from src.brains.router import BrainRouter
            squad = get_squad()
            if squad is None:
                # Bootstrap a fresh squad with default router
                router = BrainRouter()
                squad = AgentSquad(router)
            asyncio.create_task(_run_squad_with_hooks(squad, task))
            return {"ok": True, "task": task, "msg": "Squad lanzado"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── MEMORY / MEMPALACE ──────────────────────────────────
    @app.get("/api/memory")
    async def get_memory(q: str = "", limit: int = 10) -> Dict[str, Any]:
        """MemPalace stats and search."""
        try:
            from src.context.mempalace_memory import palace_count, search_memories, get_all_memories
            count = await palace_count()
            if q:
                results = await search_memories(q, n=limit)
            else:
                results = await get_all_memories(limit=limit)
            return {"ok": True, "count": count, "results": results}
        except Exception as e:
            return {"ok": False, "count": 0, "results": [], "error": str(e)}

    @app.delete("/api/memory")
    async def clear_memory() -> Dict[str, Any]:
        """Clear all MemPalace memories."""
        try:
            from src.context.mempalace_memory import delete_all_memories
            ok = await delete_all_memories()
            return {"ok": ok}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── TASKS CRUD ───────────────────────────────────────────

    from ..infra.task_store import (
        create_task as _ts_create,
        list_tasks as _ts_list,
        get_task as _ts_get,
        update_task as _ts_update,
        delete_task as _ts_delete,
        stats as _ts_stats,
    )

    @app.get("/api/tasks")
    async def get_tasks(
        status: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return all tasks from the persistent task store."""
        tasks_list = _ts_list(status=status, category=category)
        return {"tasks": tasks_list, "stats": _ts_stats()}

    @app.post("/api/tasks")
    async def create_task_endpoint(request: Request) -> Dict[str, Any]:
        """Create a new task."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title required")
        task = _ts_create(
            title=title,
            description=body.get("description", ""),
            priority=body.get("priority", "medium"),
            category=body.get("category", "fix"),
            created_by="dashboard",
            auto_fix=bool(body.get("auto_fix", False)),
            fix_command=body.get("fix_command", ""),
            tags=body.get("tags", []),
        )
        return {"ok": True, "task": task}

    @app.patch("/api/tasks/{task_id}")
    async def update_task_endpoint(task_id: str, request: Request) -> Dict[str, Any]:
        """Update fields on an existing task."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        task = _ts_update(task_id, **body)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"ok": True, "task": task}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task_endpoint(task_id: str) -> Dict[str, Any]:
        """Delete a task."""
        ok = _ts_delete(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/publish")
    async def publish_task(task_id: str, request: Request) -> Dict[str, Any]:
        """Mark task as published and record channels/URL."""
        task = _ts_get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        try:
            body = await request.json()
        except Exception:
            body = {}
        channels = body.get("channels", [])  # e.g. ["instagram","linkedin"]
        url = body.get("url", "")
        note = body.get("note", "")
        from datetime import UTC, datetime as _dt
        ts = _dt.now(UTC).isoformat()
        published_info = {
            "channels": channels,
            "url": url,
            "note": note,
            "published_at": ts,
        }
        updated = _ts_update(
            task_id,
            status="completed",
            result=(task.get("result") or "") + f"\n\n[Publicado: {', '.join(channels)} — {ts}]",
            published_channels=channels,
            published_at=ts,
            published_url=url,
        )
        # Notify Telegram
        import os as _os, httpx as _httpx
        token = _os.environ.get("TELEGRAM_BOT_TOKEN", "")
        allowed_raw = _os.environ.get("ALLOWED_USERS", "")
        if token and allowed_raw:
            chat_ids = [u.strip() for u in allowed_raw.split(",") if u.strip().isdigit()]
            ch_str = ", ".join(channels) if channels else "sin canal"
            msg = f"📣 <b>Publicado:</b> {task.get('title','')[:60]}\n📍 Canales: {ch_str}"
            if url:
                msg += f"\n🔗 {url}"
            async with _httpx.AsyncClient(timeout=8) as _client:
                for cid in chat_ids[:3]:
                    try:
                        await _client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": int(cid), "text": msg, "parse_mode": "HTML"},
                        )
                    except Exception:
                        pass
        return {"ok": True, "published": published_info}

    @app.post("/api/social/generate")
    async def social_generate(request: Request) -> Dict[str, Any]:
        """Generate social media content (caption + brand image) without posting.
        Body: {topic, headline?, subheadline?, platforms, format, width, height, count}
        Uses brand image_gen for instant on-brand images, FLUX.1 as optional background.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        topic = (body.get("topic") or "Claude AI tips").strip()
        headline = (body.get("headline") or "").strip()
        subheadline = (body.get("subheadline") or "").strip()
        platforms = body.get("platforms", ["instagram"])
        fmt = body.get("format", "1:1")
        platform = platforms[0] if platforms else "instagram"
        try:
            import base64
            from pathlib import Path as _Path
            from datetime import datetime, timezone
            from src.social.image_gen import PostSpec, generate_post_image, save_post_image
            from src.workflows.social_post import generate_post_content

            # Generate structured post content via Gemini CMO prompt
            content = await generate_post_content(topic, platform)

            # Allow caller to override specific fields
            spec = PostSpec(
                headline=headline or content["headline"],
                subheadline=subheadline or content["subheadline"],
                caption=content["caption"],
                tag=content["tag"],
                format=fmt,
            )
            png_bytes = generate_post_image(spec)

            # Save to drafts dir
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            drafts_dir = _Path.home() / ".aura" / "social_drafts"
            drafts_dir.mkdir(parents=True, exist_ok=True)
            img_path = drafts_dir / f"{platform}_{fmt.replace(':','')}_{ts}.png"
            save_post_image(png_bytes, img_path)

            # Return data URL for immediate preview in dashboard
            b64 = base64.b64encode(png_bytes).decode()
            image_data_url = f"data:image/png;base64,{b64}"

            return {
                "ok": True,
                "caption": content["caption"],
                "headline": spec.headline,
                "subheadline": spec.subheadline,
                "image_url": image_data_url,
                "image_path": str(img_path),
                "image_size_kb": len(png_bytes) // 1024,
                "topic": topic,
                "platform": platform,
                "format": fmt,
            }
        except Exception as e:
            return {"ok": False, "caption": None, "image_url": None, "error": str(e)}

    @app.post("/api/social/post")
    async def social_post_content(request: Request) -> Dict[str, Any]:
        """Generate image + caption and post via N8N (or save draft).
        Body: {text, platforms, format, width, height, topic?}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        text = (body.get("text") or "").strip()
        topic = body.get("topic") or text or "social media post"
        platforms = body.get("platforms", ["instagram"])
        fmt = body.get("format", "1:1")
        width = body.get("width", 1080)
        height = body.get("height", 1080)
        if not text and not topic:
            raise HTTPException(status_code=400, detail="text or topic required")
        try:
            from src.workflows.social_post import (
                generate_images_for_post, generate_captions,
                post_to_social, build_n8n_payload,
            )
            import os
            platform = platforms[0] if platforms else "instagram"
            style = f"social media {fmt}, dark background #141413, orange accent #d97757, professional, {width}x{height}"
            count = 1
            images = await generate_images_for_post(topic, count, style)
            captions_list = await generate_captions(topic, images, platform, style)
            caption = captions_list[0] if captions_list else text
            ok_images = [img for img in images if not img.get("error")]
            image_url = ok_images[0]["url"] if ok_images else None
            n8n_url = os.environ.get("RUD_N8N_URL", "")
            result = {"success": False, "error": "N8N not configured", "draft_saved": ""}
            if n8n_url:
                result = await post_to_social(platform, "post", topic, ok_images, [caption], n8n_url)
            else:
                # Save draft locally
                from pathlib import Path
                import json as _json
                from datetime import datetime, timezone
                drafts_dir = Path.home() / ".aura" / "social_drafts"
                drafts_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                draft_file = drafts_dir / f"{platform}_{ts}.json"
                draft_data = {"platform": platform, "type": "post", "caption": caption, "image_url": image_url, "topic": topic, "format": fmt, "timestamp": ts}
                draft_file.write_text(_json.dumps(draft_data, ensure_ascii=False, indent=2))
                result = {"success": False, "error": f"N8N no configurado — borrador guardado en {draft_file}", "draft_saved": str(draft_file)}
            return {
                "ok": result["success"],
                "image_url": image_url,
                "caption": caption,
                "platform": platform,
                "draft_saved": result.get("draft_saved", ""),
                "post_url": result.get("post_url", ""),
                "error": result.get("error", "") if not result["success"] else "",
            }
        except Exception as e:
            return {"ok": False, "image_url": None, "error": str(e)}

    # ── INSTAGRAM OAUTH ──────────────────────────────────────

    @app.get("/auth/instagram")
    async def instagram_auth_redirect(request: Request) -> Any:
        """Redirect to Instagram OAuth. Called by /ig-auth Telegram command.
        Query params: app_id, app_secret, scope (optional)
        """
        from fastapi.responses import RedirectResponse
        from urllib.parse import urlencode
        import os as _os

        app_id = request.query_params.get("app_id") or _os.environ.get("META_APP_ID", "")
        # Store secret in memory for callback (short-lived, localhost only)
        _app_secret = request.query_params.get("app_secret", "")
        if _app_secret:
            _ig_oauth_state["app_secret"] = _app_secret
            _ig_oauth_state["app_id"] = app_id

        scope = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments,instagram_business_manage_insights"
        redirect_uri = f"http://localhost:{settings.api_server_port}/auth/instagram/callback"

        params = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "enable_fb_login": "0",
        }
        auth_url = f"https://www.instagram.com/oauth/authorize?{urlencode(params)}"
        return RedirectResponse(url=auth_url)

    @app.get("/auth/instagram/callback")
    async def instagram_oauth_callback(request: Request) -> Any:
        """Handle Instagram OAuth callback. Exchanges code for long-lived token."""
        from fastapi.responses import HTMLResponse
        import aiohttp as _aiohttp
        import os as _os

        code = request.query_params.get("code", "")
        error = request.query_params.get("error", "")

        if error:
            return HTMLResponse(f"<h2>❌ Error: {error}</h2><p>Cierra esta ventana y vuelve a intentar.</p>")

        if not code:
            return HTMLResponse("<h2>❌ No se recibió código</h2>")

        app_id = _ig_oauth_state.get("app_id") or _os.environ.get("META_APP_ID", "")
        app_secret = _ig_oauth_state.get("app_secret", "")
        redirect_uri = f"http://localhost:{settings.api_server_port}/auth/instagram/callback"

        if not app_secret:
            return HTMLResponse("<h2>❌ App secret no configurado</h2><p>Usa /ig-auth con el secret del app.</p>")

        try:
            # Step 1: Exchange code → short-lived token
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(
                    "https://api.instagram.com/oauth/access_token",
                    data={
                        "client_id": app_id,
                        "client_secret": app_secret,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri,
                        "code": code,
                    },
                ) as resp:
                    token_data = await resp.json()

            if "error_type" in token_data or "access_token" not in token_data:
                return HTMLResponse(f"<h2>❌ Token exchange failed</h2><pre>{token_data}</pre>")

            short_token = token_data["access_token"]
            ig_user_id = str(token_data.get("user_id", ""))

            # Step 2: Exchange short-lived → long-lived (60 days)
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    "https://graph.instagram.com/access_token",
                    params={
                        "grant_type": "ig_exchange_token",
                        "client_secret": app_secret,
                        "access_token": short_token,
                    },
                ) as resp:
                    long_token_data = await resp.json()

            long_token = long_token_data.get("access_token", short_token)
            expires_in = long_token_data.get("expires_in", 5183944)  # ~60 days

            # Save token + credentials to .env and token file
            from pathlib import Path as _Path
            import json as _json
            from datetime import datetime as _dt, timezone as _tz

            token_info = {
                "access_token": long_token,
                "user_id": ig_user_id,
                "app_id": app_id,
                "app_secret": app_secret,
                "expires_in": expires_in,
                "created_at": _dt.now(_tz.utc).isoformat(),
                "type": "instagram_login",
                "scopes": ["instagram_business_basic", "instagram_business_content_publish",
                           "instagram_business_manage_comments", "instagram_business_manage_insights"],
            }
            token_path = _Path.home() / ".aura" / "instagram_token.json"
            token_path.write_text(_json.dumps(token_info, indent=2))

            # Update .env file
            _update_env_instagram_token(long_token, ig_user_id)

            # Store in state for immediate use
            _ig_oauth_state["token"] = long_token
            _ig_oauth_state["user_id"] = ig_user_id

            logger.info("instagram_oauth_complete", user_id=ig_user_id, expires_in=expires_in)

            # Notify via Telegram if bot is available
            asyncio.create_task(_notify_ig_auth_success(ig_user_id, expires_in))

            return HTMLResponse(f"""
            <html><body style="font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center;">
            <h1>✅ Instagram conectado</h1>
            <p>Token guardado. AURA puede publicar en Instagram.</p>
            <p style="color:#888">User ID: {ig_user_id}</p>
            <p style="color:#888">Expira en: {expires_in // 86400} días</p>
            <p><b>Cierra esta ventana.</b></p>
            </body></html>
            """)

        except Exception as exc:
            logger.error("instagram_oauth_error", error=str(exc))
            return HTMLResponse(f"<h2>❌ Error</h2><pre>{exc}</pre>")

    @app.get("/auth/instagram/refresh")
    async def instagram_token_refresh() -> Dict[str, Any]:
        """Refresh the Instagram long-lived token (call before expiry)."""
        import aiohttp as _aiohttp
        from pathlib import Path as _Path
        import json as _json

        token_path = _Path.home() / ".aura" / "instagram_token.json"
        if not token_path.exists():
            return {"ok": False, "error": "No token saved"}

        info = _json.loads(token_path.read_text())
        token = info.get("access_token", "")

        async with _aiohttp.ClientSession() as sess:
            async with sess.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
            ) as resp:
                data = await resp.json()

        if "access_token" in data:
            info["access_token"] = data["access_token"]
            info["expires_in"] = data.get("expires_in", 5183944)
            from datetime import datetime as _dt, timezone as _tz
            info["refreshed_at"] = _dt.now(_tz.utc).isoformat()
            token_path.write_text(_json.dumps(info, indent=2))
            _update_env_instagram_token(data["access_token"], info.get("user_id", ""))
            return {"ok": True, "expires_in": data.get("expires_in"), "message": "Token refreshed"}
        return {"ok": False, "error": str(data)}

    @app.post("/api/tasks/evaluate")
    async def trigger_evaluation() -> Dict[str, Any]:
        """Force AURA to run self-evaluation now (create tasks from system state)."""
        from ..infra import auto_executor
        auto_executor._LAST_EVAL = 0.0  # reset timer so self_evaluate runs
        asyncio.ensure_future(auto_executor.self_evaluate())
        return {"ok": True, "message": "Self-evaluation triggered"}

    # ── SHELL EXECUTE ────────────────────────────────────────

    @app.post("/api/shell")
    async def shell_execute(request: Request) -> Dict[str, Any]:
        """Execute a shell command and return output. For dashboard terminal."""
        import asyncio as _aio
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        cmd = (body.get("cmd") or "").strip()
        if not cmd:
            raise HTTPException(status_code=400, detail="cmd required")
        # Safety: block destructive ops in the API
        blocked = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", "shutdown", "reboot"]
        if any(b in cmd for b in blocked):
            return {"ok": False, "output": "⛔ Command blocked for safety", "exit_code": 1}
        try:
            proc = await _aio.create_subprocess_shell(
                cmd,
                stdout=_aio.subprocess.PIPE,
                stderr=_aio.subprocess.STDOUT,
                cwd=str(Path.home()),
            )
            timeout = int(body.get("timeout", 30))
            out, _ = await _aio.wait_for(proc.communicate(), timeout=timeout)
            text = _strip_ansi(out.decode(errors="replace").strip())
            return {"ok": proc.returncode == 0, "output": text[:8000], "exit_code": proc.returncode}
        except _aio.TimeoutError:
            return {"ok": False, "output": f"⏱ Timeout after {timeout}s", "exit_code": 124}
        except Exception as e:
            return {"ok": False, "output": str(e), "exit_code": 1}

    @app.post("/api/tasks/{task_id}/run")
    async def run_task_now_v2(task_id: str) -> Dict[str, Any]:
        """Execute a task's fix_command immediately. Returns 400 if no command."""
        task = _ts_get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        cmd = (task.get("fix_command") or "").strip()
        if not cmd:
            return {
                "ok": False,
                "error": "no_command",
                "message": "Tarea manual — sin fix_command. Resuélvela tú o añade un comando.",
            }
        if task.get("status") == "in_progress":
            return {"ok": False, "error": "already_running", "message": "Task is already running."}

        import asyncio as _aio

        async def _execute() -> None:
            from ..infra.auto_executor import _run_bash
            _ts_update(task_id, status="in_progress", attempts=(task.get("attempts", 0) + 1))
            ok, output = await _run_bash(cmd, timeout=120)
            from datetime import UTC, datetime as _dt
            ts = _dt.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            if ok:
                _ts_update(task_id, status="completed", result=f"[{ts}] ✅\n{output}")
            else:
                _ts_update(task_id, status="failed", result=f"[{ts}] ❌\n{output}")

        _aio.ensure_future(_execute())
        return {"ok": True, "message": "Execution started — poll /api/tasks for status."}

    # ── INVOKE TOOL ──────────────────────────────────────────

    @app.post("/api/invoke")
    async def invoke_tool(request: Request) -> Dict[str, Any]:
        """Invoke an AURA registered tool by name with args."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        tool_name = body.get("tool", "")
        kwargs = body.get("args", {})
        if not tool_name:
            raise HTTPException(status_code=400, detail="tool field required")
        try:
            from ..actions.registry import call_tool

            result = await call_tool(tool_name, **kwargs)
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── WEBHOOKS ─────────────────────────────────────────────

    @app.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str,
        request: Request,
        x_hub_signature_256: Optional[str] = Header(None),
        x_github_event: Optional[str] = Header(None),
        x_github_delivery: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Receive and validate webhook from an external provider."""
        body = await request.body()

        if provider == "github":
            secret = settings.github_webhook_secret
            if not secret:
                raise HTTPException(status_code=500, detail="GitHub webhook secret not configured")
            if not verify_github_signature(body, x_hub_signature_256, secret):
                logger.warning("GitHub webhook signature verification failed", delivery_id=x_github_delivery)
                raise HTTPException(status_code=401, detail="Invalid signature")
            event_type_name = x_github_event or "unknown"
            delivery_id = x_github_delivery or str(uuid.uuid4())
        else:
            secret = settings.webhook_api_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail="Webhook API secret not configured. Set WEBHOOK_API_SECRET.",
                )
            if not verify_shared_secret(authorization, secret):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID", str(uuid.uuid4()))

        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            payload = {"raw_body": body.decode("utf-8", errors="replace")[:5000]}

        if db_manager and delivery_id:
            is_new = await _try_record_webhook(
                db_manager,
                event_id=str(uuid.uuid4()),
                provider=provider,
                event_type=event_type_name,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not is_new:
                logger.info("Duplicate webhook delivery ignored", provider=provider, delivery_id=delivery_id)
                return {"status": "duplicate", "delivery_id": delivery_id}

        event = WebhookEvent(
            provider=provider,
            event_type_name=event_type_name,
            payload=payload,
            delivery_id=delivery_id,
        )
        await event_bus.publish(event)
        logger.info("Webhook received and published", provider=provider, event_type=event_type_name)
        return {"status": "accepted", "event_id": event.id}

    # ── SQLITE REAL STATS ────────────────────────────────────

    @app.get("/api/sqlite/stats")
    async def get_sqlite_stats() -> Dict[str, Any]:
        """Real usage stats from SQLite — sessions, messages, costs, tools."""
        db_path = Path(__file__).parent.parent.parent / "data" / "bot.db"
        if not db_path.exists():
            return {"error": "No database found", "sessions": 0}
        try:
            import aiosqlite
            async with aiosqlite.connect(str(db_path)) as conn:
                conn.row_factory = aiosqlite.Row

                # Sessions
                cur = await conn.execute(
                    "SELECT COUNT(*) as cnt, COALESCE(SUM(total_cost),0) as cost, "
                    "COALESCE(SUM(total_turns),0) as turns, "
                    "COALESCE(SUM(message_count),0) as msgs, "
                    "MAX(last_used) as last_used FROM sessions"
                )
                s = dict(await cur.fetchone())

                # Messages
                cur = await conn.execute(
                    "SELECT COUNT(*) as cnt, "
                    "COALESCE(SUM(cost),0) as cost, "
                    "COALESCE(AVG(duration_ms),0) as avg_ms "
                    "FROM messages WHERE error IS NULL OR error=''"
                )
                m = dict(await cur.fetchone())

                # Top tools
                cur = await conn.execute(
                    "SELECT tool_name, COUNT(*) as cnt FROM tool_usage "
                    "GROUP BY tool_name ORDER BY cnt DESC LIMIT 12"
                )
                tools = [dict(r) for r in await cur.fetchall()]

                # Recent messages
                cur = await conn.execute(
                    "SELECT prompt, response, cost, duration_ms, timestamp "
                    "FROM messages ORDER BY timestamp DESC LIMIT 10"
                )
                recent = []
                for r in await cur.fetchall():
                    d = dict(r)
                    d["prompt"] = (d["prompt"] or "")[:120]
                    d["response"] = (d["response"] or "")[:200]
                    recent.append(d)

                return {
                    "sessions": s,
                    "messages": m,
                    "tools": tools,
                    "recent_messages": recent,
                }
        except Exception as e:
            return {"error": str(e)}

    # ── RUD SERVER STATUS ────────────────────────────────────

    @app.get("/api/rud-server")
    async def rud_server_status() -> Dict[str, Any]:
        """Return status and available models for the RUD remote server."""
        import os as _os
        import httpx as _httpx

        ollama_url = _os.environ.get("RUD_OLLAMA_URL", "http://192.168.1.219:11434").rstrip("/")
        n8n_url = _os.environ.get("RUD_N8N_URL", "http://192.168.1.219:5678").rstrip("/")
        grafana_url = _os.environ.get("RUD_GRAFANA_URL", "http://192.168.1.219:3200").rstrip("/")
        portainer_url = _os.environ.get("RUD_PORTAINER_URL", "https://192.168.1.219:9443").rstrip("/")

        async def _check(url: str, path: str = "/") -> bool:
            try:
                async with _httpx.AsyncClient(timeout=3.0, verify=False) as client:
                    r = await client.get(url + path)
                    return r.status_code < 500
            except Exception:
                return False

        # Check Ollama and grab model list
        models: List[str] = []
        ollama_online = False
        try:
            async with _httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{ollama_url}/api/tags")
                if r.status_code == 200:
                    ollama_online = True
                    data = r.json()
                    models = [m["name"] for m in data.get("models", [])]
        except Exception:
            ollama_online = False

        # Check other services in parallel
        n8n_ok, grafana_ok = await asyncio.gather(
            _check(n8n_url),
            _check(grafana_url),
        )

        return {
            "online": ollama_online or n8n_ok or grafana_ok,
            "ollama_url": ollama_url,
            "models": models,
            "services": {
                "ollama": {"online": ollama_online, "url": ollama_url},
                "n8n": {"online": n8n_ok, "url": n8n_url},
                "grafana": {"online": grafana_ok, "url": grafana_url},
                "portainer": {"online": False, "url": portainer_url},  # checked on demand
            },
        }

    # ── TERMORA TERMINAL URL ──────────────────────────────────

    @app.get("/api/terminal")
    async def terminal_info() -> Dict[str, Any]:
        """Return Termora auth URL for dashboard iframe embedding."""
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get("http://localhost:4030/api/info")
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "online": True,
                        "tunnelUrl": data.get("tunnelUrl"),
                        "authUrl": data.get("authUrl"),
                        "tunnelMethod": data.get("tunnelMethod", "local"),
                        "machineName": data.get("machineName", ""),
                    }
        except Exception:
            pass
        return {"online": False, "authUrl": None, "tunnelUrl": None, "tunnelMethod": None, "machineName": None}

    # ── DASHBOARD PUBLIC URL ──────────────────────────────────

    @app.get("/api/dashboard-url")
    async def dashboard_url_info() -> Dict[str, Any]:
        """Return the public dashboard URL served via cloudflared tunnel."""
        from ..infra.tunnel import get_dashboard_url
        url = get_dashboard_url()
        dashboard_url_file = Path.home() / ".aura" / "dashboard_url.txt"
        # Fall back to file on disk (survives restarts)
        if not url and dashboard_url_file.exists():
            try:
                url = dashboard_url_file.read_text(encoding="utf-8").strip() or None
            except Exception:
                url = None
        return {
            "url": url,
            "online": url is not None,
            "port": settings.api_server_port,
        }

    # ── SSE LIVE LOG STREAM ───────────────────────────────────

    from fastapi.responses import StreamingResponse as _StreamingResponse

    @app.get("/api/stream/logs")
    async def stream_logs() -> _StreamingResponse:
        """Server-Sent Events stream of live log lines."""
        import asyncio as _aio

        log_path = Path.home() / "claude-code-telegram/logs/bot.stdout.log"

        async def _gen():
            # Tail the log file from the end
            pos = 0
            if log_path.exists():
                pos = log_path.stat().st_size

            import json as _j
            import time as _t
            last_heartbeat = _t.time()

            while True:
                try:
                    if log_path.exists():
                        size = log_path.stat().st_size
                        if size > pos:
                            with open(log_path, "rb") as f:
                                f.seek(pos)
                                chunk = f.read(size - pos)
                            pos = size
                            for line in chunk.decode(errors="replace").splitlines():
                                clean = _strip_ansi(line).strip()
                                if not clean:
                                    continue
                                cl = clean.lower()
                                lvl = ("error" if "error" in cl else
                                       "warning" if "warn" in cl else
                                       "debug" if "debug" in cl else "info")
                                data = _j.dumps({"text": clean[:500], "level": lvl,
                                                 "ts": _t.time()})
                                yield f"data: {data}\n\n"

                    # Heartbeat every 15s to keep connection alive
                    now = _t.time()
                    if now - last_heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now

                except _aio.CancelledError:
                    return  # Client disconnected — exit cleanly
                except Exception:
                    pass
                await _aio.sleep(0.5)

        return _StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── CHAT VIA BRAIN ROUTER ─────────────────────────────────

    @app.post("/api/chat")
    async def chat_with_brain(request: Request) -> Dict[str, Any]:
        """Send a message through the brain router. Returns response + metadata."""
        import asyncio as _aio
        import time as _time
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")

        brain_name = (body.get("brain") or "").strip() or None
        working_dir = body.get("working_dir") or str(Path.home())

        router = brain_router
        if not router:
            return {"ok": False, "error": "Brain router not available"}

        t0 = _time.time()
        try:
            # Pick brain
            if brain_name:
                brain = router.get_brain(brain_name)
            else:
                # Auto-route via smart_route (rate-aware)
                auto_name, _ = router.smart_route(message, rate_monitor=rate_monitor)
                brain = router.get_brain(auto_name) if auto_name != "zero-token" else router.get_brain("gemini")
                brain_name = brain.name if brain else "unknown"

            if not brain:
                return {"ok": False, "error": f"Brain '{brain_name}' not found"}

            response = await brain.execute(
                prompt=message,
                working_directory=working_dir,
            )

            if rate_monitor and not response.is_error:
                rate_monitor.record_request(brain.name)
            elif rate_monitor and response.is_error:
                is_rl = "rate" in (response.error_type or "").lower()
                rate_monitor.record_error(brain.name, is_rate_limit=is_rl)

            duration_ms = int((_time.time() - t0) * 1000)
            return {
                "ok": not response.is_error,
                "brain": brain.name,
                "brain_display": getattr(brain, "display_name", brain.name),
                "content": response.content or "(sin respuesta)",
                "error": response.error_type if response.is_error else None,
                "duration_ms": duration_ms,
                "cost": response.cost,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "brain": brain_name or "?"}

    # ── FULL ROUTER STATUS (all brains + rate monitor merge) ─────

    @app.get("/api/router")
    async def get_router_status() -> Dict[str, Any]:
        """All brains from brain router with rate-monitor data merged."""
        import time as _time
        if not brain_router:
            return {"brains": [], "cascade": [], "error": "router not ready"}

        _CASCADE = [
            "api-zero", "ollama-rud", "qwen-code", "opencode",
            "gemini", "openrouter", "cline", "codex",
            "haiku", "sonnet", "opus", "image",
        ]

        # Rate monitor snapshot
        rate_data: Dict[str, Any] = {}
        try:
            from ..infra.rate_monitor import RateMonitor
            monitor = RateMonitor()
            for u in monitor.get_all_usage():
                rate_data[u.brain_name] = u
        except Exception:
            pass

        brains = []
        for rank, name in enumerate(_CASCADE, 1):
            brain = brain_router.get_brain(name)
            if not brain:
                continue
            u = rate_data.get(name)
            pct = round(u.usage_pct * 100, 1) if u and u.usage_pct is not None else None
            warn_t = 0.75
            is_rl = bool(u and u.is_rate_limited)
            status = "rate_limited" if is_rl else ("warn" if pct and pct >= warn_t * 100 else "ok")
            brains.append({
                "name": name,
                "rank": rank,
                "display_name": getattr(brain, "display_name", name),
                "emoji": getattr(brain, "emoji", "●"),
                "cost": getattr(brain, "cost", "free"),
                "requests": u.requests_in_window if u else 0,
                "limit": u.known_limit if u else None,
                "usage_pct": pct,
                "window": getattr(u, "window_seconds", None),
                "window_remaining": u.window_remaining_str if u else None,
                "errors": u.errors_in_window if u else 0,
                "is_rate_limited": is_rl,
                "status": status,
            })

        # Cascade intent map (current routing targets)
        _INTENT_MAP = {
            "BASH": "zero-token", "FILES": "zero-token", "GIT": "zero-token",
            "CHAT": "qwen-code", "DEEP": "qwen-code", "TRANSLATE": "qwen-code",
            "CODE": "ollama-rud", "SEARCH": "gemini",
            "EMAIL": "haiku", "CALENDAR": "haiku",
        }

        return {
            "brains": brains,
            "total": len(brains),
            "available": sum(1 for b in brains if b["status"] == "ok"),
            "intent_map": _INTENT_MAP,
            "ts": _time.time(),
        }

    # ── SSE ORCHESTRATION STREAM ─────────────────────────────

    @app.get("/api/stream/orchestration")
    async def stream_orchestration() -> _StreamingResponse:
        """Server-Sent Events stream of live conductor/orchestration events.

        Events: planning, plan_created, step_started, step_completed,
                step_failed, run_completed, run_failed
        """
        import asyncio as _aio
        import json as _j
        import time as _t
        from ..brains.conductor import orch_subscribe, orch_unsubscribe

        async def _gen():
            q = orch_subscribe()
            last_hb = _t.time()
            try:
                # Send immediate connected event with current system state
                # so the browser gets something right away (not blank for 15s)
                connected_event = {
                    "type": "connected",
                    "ts": _t.time(),
                    "uptime_s": int(_t.time()),
                }
                try:
                    from ..infra.proactive_loop import get_proactive_status
                    ps = get_proactive_status()
                    connected_event["proactive"] = {
                        "running": ps.get("running", False),
                        "last_run_at": ps.get("last_run_at"),
                        "next_run_at": ps.get("next_run_at"),
                        "total_runs": ps.get("total_runs", 0),
                        "last_result": ps.get("last_result"),
                    }
                except Exception:
                    pass
                yield f"data: {_j.dumps(connected_event)}\n\n"

                while True:
                    try:
                        event = q.get_nowait()
                        yield f"data: {_j.dumps(event)}\n\n"
                    except _aio.QueueEmpty:
                        pass

                    now = _t.time()
                    if now - last_hb >= 5:  # heartbeat every 5s (was 15s)
                        yield ": heartbeat\n\n"
                        last_hb = now

                    await _aio.sleep(0.1)
            except _aio.CancelledError:
                orch_unsubscribe(q)
                return

        return _StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── CONDUCTOR RUN ─────────────────────────────────────────

    @app.post("/api/conductor/run")
    async def conductor_run(request: Request) -> Dict[str, Any]:
        """Trigger a 3-layer conductor run from the dashboard.

        Body: {task: str, async: bool}
        If async=true, returns immediately and streams events via /api/stream/orchestration.
        If async=false (default), waits for completion and returns full result.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        task = (body.get("task") or "").strip()
        if not task:
            raise HTTPException(status_code=400, detail="task required")

        run_async = bool(body.get("async", True))

        if not brain_router:
            return {"ok": False, "error": "Brain router not available"}

        from ..brains.conductor import get_conductor, Conductor, set_conductor
        conductor = get_conductor(brain_router)
        if conductor is None:
            conductor = Conductor(brain_router)
            set_conductor(conductor)

        if run_async:
            import uuid as _uuid
            run_id = str(_uuid.uuid4())[:8]
            asyncio.create_task(conductor.run(task, run_id=run_id, source="manual"))
            return {"ok": True, "run_id": run_id, "task": task,
                    "stream": "/api/stream/orchestration"}
        else:
            try:
                result = await asyncio.wait_for(conductor.run(task, source="manual"), timeout=300)
                return {
                    "ok": not result.is_error,
                    "run_id": result.run_id,
                    "task": task,
                    "output": result.final_output,
                    "steps_completed": result.steps_completed,
                    "steps_failed": result.steps_failed,
                    "duration_ms": result.total_duration_ms,
                }
            except asyncio.TimeoutError:
                return {"ok": False, "error": "timeout (300s)", "task": task}
            except Exception as e:
                return {"ok": False, "error": str(e), "task": task}

    @app.get("/api/conductor/status")
    async def conductor_status() -> Dict[str, Any]:
        """Return whether a conductor is initialized and available."""
        from ..brains.conductor import get_conductor, _subscribers
        c = get_conductor()
        return {
            "available": c is not None,
            "sse_subscribers": len(_subscribers),
            "stream_url": "/api/stream/orchestration",
        }

    # ── CONDUCTOR HISTORY ─────────────────────────────────────

    @app.get("/api/conductor/history")
    async def conductor_history_endpoint() -> Dict[str, Any]:
        """Return recent conductor run history for the Sessions panel."""
        try:
            from ..infra.conductor_history import get_history, history_stats
            runs = get_history(limit=50)
            return {"ok": True, "runs": runs, "stats": history_stats()}
        except Exception as e:
            return {"ok": False, "runs": [], "stats": {}, "error": str(e)}

    @app.get("/api/conductor/metrics")
    async def conductor_metrics_endpoint() -> Dict[str, Any]:
        """Return conductor metrics: success rates by layer and brain, best brain, avg durations."""
        try:
            from ..infra.conductor_history import conductor_metrics
            return {"ok": True, "metrics": conductor_metrics()}
        except Exception as e:
            return {"ok": False, "metrics": {}, "error": str(e)}

    @app.get("/api/learnings")
    async def get_learnings(days: int = 7, limit: int = 100) -> Dict[str, Any]:
        """Parse conductor_log.md and return learning entries from the past N days."""
        import re as _re
        from datetime import UTC, datetime, timedelta

        log_path = Path.home() / ".aura" / "memory" / "conductor_log.md"
        try:
            if not log_path.exists():
                return {"ok": True, "entries": [], "stats": {"total": 0, "success": 0, "failed": 0, "success_rate": 0, "top_brains": [], "days": days}, "note": "No hay learnings registrados aún"}

            text = log_path.read_text(encoding="utf-8")
            cutoff = datetime.now(UTC) - timedelta(days=days)

            raw_blocks = _re.split(r"\n(?=## \d{4}-\d{2}-\d{2})", text)
            entries: List[Dict[str, Any]] = []

            for block in raw_blocks:
                block = block.strip()
                if not block.startswith("## "):
                    continue
                first_line = block.split("\n")[0]
                header_m = _re.match(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — (.+)$", first_line)
                if not header_m:
                    continue
                ts_str, status_str = header_m.group(1), header_m.group(2)
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                except ValueError:
                    continue
                if ts < cutoff:
                    continue

                def _field(name: str, b: str = block) -> str:
                    m = _re.search(rf"\*\*{name}\*\*: (.+)", b)
                    return m.group(1).strip() if m else ""

                entries.append({
                    "timestamp": ts_str,
                    "status": "success" if "SUCCESS" in status_str else "failed",
                    "task": _field("Task"),
                    "strategy": _field("Strategy"),
                    "duration": _field("Duration"),
                    "steps": _field("Steps"),
                    "brains": _field("Brains"),
                    "layers": _field("Layers"),
                    "run_id": _field("Run ID").strip("`"),
                    "error": _field("Error"),
                    "failed_brains": _field("Failed brains"),
                })

            entries = list(reversed(entries))[:limit]

            total = len(entries)
            success = sum(1 for e in entries if e["status"] == "success")
            failed = total - success

            brain_freq: Dict[str, int] = {}
            for e in entries:
                for part in e["brains"].split(","):
                    b = part.strip().split("×")[0].strip()
                    if b:
                        brain_freq[b] = brain_freq.get(b, 0) + 1
            top_brains = sorted(brain_freq.items(), key=lambda x: -x[1])[:5]

            return {
                "ok": True,
                "entries": entries,
                "stats": {
                    "total": total,
                    "success": success,
                    "failed": failed,
                    "success_rate": round(100 * success / total) if total else 0,
                    "top_brains": [{"brain": b, "count": c} for b, c in top_brains],
                    "days": days,
                },
            }
        except Exception as e:
            return {"ok": False, "entries": [], "stats": {}, "error": str(e)}

    @app.get("/api/proactive/status")
    async def proactive_status_endpoint() -> Dict[str, Any]:
        """Return proactive loop status: running, last/next run, stats."""
        try:
            from ..infra.proactive_loop import get_proactive_status
            return {"ok": True, **get_proactive_status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── USAGE STATS ───────────────────────────────────────────

    @app.get("/api/usage")
    async def get_usage_stats(days: int = 0) -> Dict[str, Any]:
        """Return activity heatmap, streaks, peak hour, and model breakdown.
        days=0 → all time; days=30/7 → last N days.
        """
        import re as _re
        from datetime import date, timedelta, datetime as _dt

        try:
            # ── SQLite activity ──────────────────────────────────────
            db_path = settings.database_path
            daily: Dict[str, int] = {}
            hourly: Dict[str, int] = {}
            total_msgs = 0
            total_sessions = 0
            peak_hour = "—"
            fav_model = "haiku"

            if db_path and Path(db_path).exists():
                import aiosqlite
                async with aiosqlite.connect(str(db_path)) as _db:
                    cutoff = ""
                    if days > 0:
                        since = (date.today() - timedelta(days=days)).isoformat()
                        cutoff = f" WHERE date(timestamp) >= '{since}'"

                    # Per-day counts
                    async with _db.execute(
                        f"SELECT date(timestamp), COUNT(*) FROM messages{cutoff} GROUP BY date(timestamp)"
                    ) as cur:
                        async for row in cur:
                            daily[row[0]] = row[1]

                    # Hourly counts
                    async with _db.execute(
                        f"SELECT strftime('%H', timestamp), COUNT(*) FROM messages{cutoff} GROUP BY 1 ORDER BY 2 DESC"
                    ) as cur:
                        rows = await cur.fetchall()
                        if rows:
                            peak_hour = str(int(rows[0][0]))
                            for r in rows:
                                hourly[r[0]] = r[1]

                    # Totals
                    async with _db.execute("SELECT COUNT(*) FROM messages") as cur:
                        row = await cur.fetchone()
                        total_msgs = (row[0] or 0) if row else 0

                    async with _db.execute("SELECT COUNT(*) FROM sessions") as cur:
                        row = await cur.fetchone()
                        total_sessions = (row[0] or 0) if row else 0

            # ── Conductor log model breakdown ────────────────────────
            log_path = Path.home() / ".aura" / "memory" / "conductor_log.md"
            model_day_counts: Dict[str, Dict[str, int]] = {}  # model → {date → count}
            model_totals: Dict[str, int] = {}

            if log_path.exists():
                log_text = log_path.read_text(errors="replace")
                # Parse blocks: "## YYYY-MM-DD HH:MM — run_id"
                blocks = _re.split(r'\n## \d{4}-\d{2}-\d{2}', log_text)
                for block in blocks:
                    date_m = _re.search(r'^(\d{4}-\d{2}-\d{2})', block)
                    day_str = date_m.group(1) if date_m else None
                    if days > 0 and day_str:
                        try:
                            since_d = date.today() - timedelta(days=days)
                            if _dt.strptime(day_str, "%Y-%m-%d").date() < since_d:
                                continue
                        except ValueError:
                            pass
                    # Count brain mentions
                    for brain in ("haiku", "sonnet", "opus", "qwen", "gemini", "granite", "openrouter", "local-ollama"):
                        if brain in block.lower():
                            cnt = block.lower().count(brain)
                            model_totals[brain] = model_totals.get(brain, 0) + cnt
                            if day_str:
                                model_day_counts.setdefault(brain, {})
                                model_day_counts[brain][day_str] = model_day_counts[brain].get(day_str, 0) + cnt
                    # Also count per-day for heatmap (conductor runs)
                    if day_str:
                        if "✅" in block or "COMMITTED" in block:
                            daily[day_str] = daily.get(day_str, 0) + 1

            # Fav model
            if model_totals:
                fav_model = max(model_totals, key=lambda k: model_totals[k])

            # ── Heatmap: last 52 weeks ───────────────────────────────
            today = date.today()
            heatmap_start = today - timedelta(weeks=52)
            heatmap: list[dict] = []
            max_day = max(daily.values(), default=1)
            cur_d = heatmap_start
            while cur_d <= today:
                ds = cur_d.isoformat()
                cnt = daily.get(ds, 0)
                level = 0 if cnt == 0 else (1 if cnt <= max_day * 0.25 else (2 if cnt <= max_day * 0.5 else (3 if cnt <= max_day * 0.75 else 4)))
                heatmap.append({"date": ds, "count": cnt, "level": level})
                cur_d += timedelta(days=1)

            # ── Streak ──────────────────────────────────────────────
            streak = 0
            longest = 0
            run = 0
            for i in range(len(heatmap) - 1, -1, -1):
                if heatmap[i]["count"] > 0:
                    run += 1
                    if i == len(heatmap) - 1 or heatmap[i + 1]["count"] == 0:
                        if streak == 0:
                            streak = run
                    longest = max(longest, run)
                else:
                    run = 0

            # ── Model bar chart: by day ──────────────────────────────
            top_models = sorted(model_totals.keys(), key=lambda k: -model_totals[k])[:6]
            total_model_uses = sum(model_totals.values()) or 1
            model_list = [
                {
                    "name": m,
                    "total": model_totals[m],
                    "pct": round(model_totals[m] / total_model_uses * 100, 1),
                    "by_day": model_day_counts.get(m, {}),
                }
                for m in top_models
            ]

            # Days with any activity
            active_days = len([d for d in daily.values() if d > 0])

            return {
                "ok": True,
                "summary": {
                    "total_sessions": total_sessions,
                    "total_messages": total_msgs,
                    "active_days": active_days,
                    "streak_current": streak,
                    "streak_longest": longest,
                    "peak_hour": peak_hour,
                    "fav_model": fav_model,
                },
                "heatmap": heatmap,
                "models": model_list,
            }
        except Exception as e:
            import traceback
            return {"ok": False, "error": str(e), "trace": traceback.format_exc()[-500:]}

    # ── CLAUDE CONTEXT WINDOW ─────────────────────────────────

    _CONTEXT_FILE = Path.home() / ".aura" / "context" / "claude_context.json"

    @app.get("/api/claude/context")
    async def get_claude_context() -> Dict[str, Any]:
        """Return stored Claude context window breakdown."""
        try:
            if _CONTEXT_FILE.exists():
                data = json.loads(_CONTEXT_FILE.read_text())
                return {"ok": True, **data}
            return {"ok": False, "error": "No context data yet"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/claude/context")
    async def update_claude_context(request: Request) -> Dict[str, Any]:
        """Update Claude context window data. Body: full context JSON."""
        try:
            body = await request.json()
            _CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CONTEXT_FILE.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            return {"ok": True, "saved": str(_CONTEXT_FILE)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── RAG endpoints ────────────────────────────────────────────────────────────

    @app.get("/api/rag/status")
    async def rag_status() -> Dict[str, Any]:
        try:
            from src.rag.retriever import RAGRetriever
            r = RAGRetriever()
            return await r.status()
        except Exception as e:
            return {"available": False, "error": str(e)}

    @app.get("/api/rag/search")
    async def rag_search(q: str, top_k: int = 5) -> Dict[str, Any]:
        try:
            from src.rag.retriever import RAGRetriever
            r = RAGRetriever()
            results = await r.search(q, top_k=top_k)
            return {"results": results, "query": q}
        except Exception as e:
            return {"results": [], "error": str(e)}

    @app.post("/api/rag/index")
    async def rag_reindex() -> Dict[str, Any]:
        """Trigger manual re-indexing of all sources."""
        try:
            from src.rag.indexer import RAGIndexer
            idx = RAGIndexer()
            stats = await idx.index_all()
            return {"ok": True, **stats}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

    # ── Routines API ────────────────────────────────────────────────────────────

    @app.get("/api/routines")
    async def get_routines() -> Dict[str, Any]:
        """List all routines."""
        try:
            from src.scheduler.routines_store import list_routines
            routines = await list_routines()
            return {"routines": [r.as_dict() for r in routines]}
        except Exception as e:
            return {"routines": [], "error": str(e)}

    @app.post("/api/routines")
    async def create_routine_endpoint(request: Request) -> Dict[str, Any]:
        """Create a new routine."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        try:
            from src.scheduler.routines_store import Routine, create_routine, routine_exists
            from src.scheduler.routine_runner import schedule_routine
            name = (body.get("name") or "").strip()
            prompt = (body.get("prompt") or "").strip()
            if not name or not prompt:
                raise HTTPException(status_code=400, detail="name and prompt required")
            if await routine_exists(name):
                raise HTTPException(status_code=409, detail=f"Routine '{name}' already exists")
            r = Routine(
                name=name,
                prompt=prompt,
                description=body.get("description") or "",
                brain=body.get("brain") or "codex",
                frequency=body.get("frequency") or "daily",
                schedule_time=body.get("schedule_time") or "09:00",
                working_dir=body.get("working_dir") or "/Users/oxyzen/claude-code-telegram",
                is_local=bool(body.get("is_local", True)),
                auto_created=bool(body.get("auto_created", False)),
            )
            await create_routine(r)
            schedule_routine(r)
            return {"ok": True, "routine": r.as_dict()}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.patch("/api/routines/{routine_id}")
    async def update_routine_endpoint(
        routine_id: str, request: Request
    ) -> Dict[str, Any]:
        """Update routine fields (name, prompt, enabled, frequency, etc.)."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        try:
            from src.scheduler.routines_store import update_routine, get_routine
            from src.scheduler.routine_runner import schedule_routine, unschedule_routine
            updated = await update_routine(routine_id, **body)
            if not updated:
                raise HTTPException(status_code=404, detail="Routine not found")
            # Re-schedule if enabled state changed
            if updated.enabled:
                schedule_routine(updated)
            else:
                unschedule_routine(routine_id)
            return {"ok": True, "routine": updated.as_dict()}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/routines/{routine_id}")
    async def delete_routine_endpoint(routine_id: str) -> Dict[str, Any]:
        """Delete a routine and remove from scheduler."""
        try:
            from src.scheduler.routines_store import delete_routine
            from src.scheduler.routine_runner import unschedule_routine
            unschedule_routine(routine_id)
            deleted = await delete_routine(routine_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Routine not found")
            return {"ok": True}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/routines/{routine_id}/run")
    async def trigger_routine(routine_id: str) -> Dict[str, Any]:
        """Trigger a routine in the background. Returns job_id immediately."""
        try:
            from src.scheduler.routine_runner import run_routine_background
            job_id = await run_routine_background(routine_id)
            return {"ok": True, "job_id": job_id, "status": "running"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/routines/jobs/{job_id}")
    async def get_routine_job(job_id: str) -> Dict[str, Any]:
        """Poll the status of a background routine run."""
        from src.scheduler.routine_runner import get_job_status
        job = get_job_status(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/api/routines/{routine_id}/logs")
    async def get_routine_logs(routine_id: str, limit: int = 20) -> Dict[str, Any]:
        """Get execution history for a routine."""
        try:
            from src.scheduler.routines_store import get_logs
            logs = await get_logs(routine_id, limit=limit)
            return {"logs": logs}
        except Exception as e:
            return {"logs": [], "error": str(e)}

    return app


async def _try_record_webhook(
    db_manager: DatabaseManager,
    event_id: str,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: Dict[str, Any],
) -> bool:
    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
            (event_id, provider, event_type, delivery_id, payload, processed)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (event_id, provider, event_type, delivery_id, json.dumps(payload)),
        )
        cursor = await conn.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = row[0] > 0 if row else False
        await conn.commit()
        return inserted


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
