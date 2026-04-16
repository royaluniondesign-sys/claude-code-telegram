"""Misc router: /api/mcp, /api/usage, /api/sqlite/*, /api/rud-server,
/api/shell, /api/terminal, /api/dashboard-url, /api/tools, /api/crons,
/api/invoke, /api/router (brains), /api/chat."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, HTTPException, Request

logger = structlog.get_logger()

_ANSI_RE = re.compile(r"\x1B\[[0-9;]*[mK]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


router = APIRouter()


@router.get("/api/mcp")
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


@router.get("/api/tools")
async def get_tools() -> Dict[str, Any]:
    """List all registered AURA tools from the action registry."""
    try:
        from ...actions.registry import registry

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


@router.get("/api/crons")
async def get_crons() -> Dict[str, Any]:
    """List scheduled workflow definitions."""
    try:
        from ...workflows.scheduler_setup import _WORKFLOW_DEFS

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


@router.get("/api/usage")
async def get_usage_stats(days: int = 0) -> Dict[str, Any]:
    """Return activity heatmap, streaks, peak hour, and model breakdown.
    days=0 → all time; days=30/7 → last N days.
    """
    import re as _re
    from datetime import date, timedelta, datetime as _dt

    try:
        # ── SQLite activity ──────────────────────────────────────
        from ...config.settings import Settings
        _settings = Settings()
        db_path = _settings.database_path
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


@router.get("/api/sqlite/stats")
async def get_sqlite_stats() -> Dict[str, Any]:
    """Real usage stats from SQLite — sessions, messages, costs, tools."""
    db_path = Path(__file__).parent.parent.parent.parent / "data" / "bot.db"
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


@router.get("/api/rud-server")
async def rud_server_status() -> Dict[str, Any]:
    """Return status and available models for the RUD remote server."""
    import httpx as _httpx

    ollama_url = (
        __import__("os").environ.get("RUD_OLLAMA_URL", "http://192.168.1.219:11434").rstrip("/")
    )
    n8n_url = __import__("os").environ.get("RUD_N8N_URL", "http://192.168.1.219:5678").rstrip("/")
    grafana_url = (
        __import__("os").environ.get("RUD_GRAFANA_URL", "http://192.168.1.219:3200").rstrip("/")
    )
    portainer_url = (
        __import__("os").environ.get("RUD_PORTAINER_URL", "https://192.168.1.219:9443").rstrip("/")
    )

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


@router.post("/api/shell")
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


@router.get("/api/terminal")
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


@router.get("/api/dashboard-url")
async def dashboard_url_info() -> Dict[str, Any]:
    """Return the public dashboard URL served via cloudflared tunnel."""
    import os as _os
    from ...infra.tunnel import get_dashboard_url
    url = get_dashboard_url()
    dashboard_url_file = Path.home() / ".aura" / "dashboard_url.txt"
    # Fall back to file on disk (survives restarts)
    if not url and dashboard_url_file.exists():
        try:
            url = dashboard_url_file.read_text(encoding="utf-8").strip() or None
        except Exception:
            url = None
    port = int(_os.environ.get("API_SERVER_PORT", "8080"))
    return {
        "url": url,
        "online": url is not None,
        "port": port,
    }


@router.post("/api/invoke")
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
        from ...actions.registry import call_tool

        result = await call_tool(tool_name, **kwargs)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}
