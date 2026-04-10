"""FastAPI webhook + dashboard API server.

Runs in the same process as the bot, sharing the event loop.
Serves the monitoring dashboard at / and API endpoints at /api/*.
"""

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

    # ── HEALTH ──────────────────────────────────────────────

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

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

        # RAM (macOS vm_stat)
        try:
            proc = await asyncio.create_subprocess_shell(
                "vm_stat | grep 'Pages free' | awk '{print $3}' | tr -d '.'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            free_pages = int(out.decode().strip() or "0")
            page_sz = os.sysconf("SC_PAGE_SIZE")
            total_b = page_sz * os.sysconf("SC_PHYS_PAGES")
            free_gb = free_pages * page_sz / 1e9
            result["system"]["ram_pct"] = round(
                (1 - (free_pages * page_sz) / total_b) * 100, 1
            )
            result["system"]["ram_free_gb"] = round(free_gb, 1)
            result["system"]["ram_total_gb"] = round(total_b / 1e9, 1)
        except Exception:
            pass

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
                "message": "This task has no auto-fix command — resolve manually or add a fix_command.",
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
                                import json as _j
                                data = _j.dumps({"text": clean[:500], "level": lvl,
                                                 "ts": __import__("time").time()})
                                yield f"data: {data}\n\n"
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
                # Auto-route via meta-router
                from ..claude.meta_router import route_request as _meta
                decision = _meta(message)
                # Map tier to brain name
                _tier_map = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"}
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

    # ── STATIC DASHBOARD ─────────────────────────────────────

    if _DASHBOARD_DIR.exists():
        app.mount("/app", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(_DASHBOARD_DIR / "index.html"))

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

    app = create_api_app(event_bus, settings, db_manager, brain_router=brain_router, rate_monitor=rate_monitor)
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=settings.api_server_port,
        log_level="info" if not settings.debug else "debug",
    )
    server = uvicorn.Server(config)
    await server.serve()
