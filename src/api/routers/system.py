"""System router: /health, /api/system, /api/status, /api/logs, /api/stream/logs."""

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = structlog.get_logger()

_ANSI_RE = re.compile(r"\x1B\[[0-9;]*[mK]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


router = APIRouter()


@router.get("/health")
async def health_check() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/api/system")
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


@router.get("/api/status")
async def get_status() -> Dict[str, Any]:
    """AURA live status — system, brains, logs."""
    import asyncio
    import os
    import re as _re
    import shutil
    import subprocess as _sp
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
                import time as _t
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
        import subprocess as _sp2

        _sysctl = "/usr/sbin/sysctl"
        _pg = int(_sp2.check_output([_sysctl, "-n", "hw.pagesize"], timeout=3).strip())
        _tb = int(_sp2.check_output([_sysctl, "-n", "hw.memsize"],  timeout=3).strip())
        _vm = _sp2.check_output("vm_stat", shell=True, timeout=3, text=True)

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
        from ...infra.rate_monitor import BRAIN_LIMITS, RateMonitor

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


@router.get("/api/logs")
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


@router.get("/api/stream/logs")
async def stream_logs() -> StreamingResponse:
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

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
