"""Brains router: /api/brains, /api/cortex, /api/learnings, /api/rate-limits,
/api/router, /api/chat."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request

logger = structlog.get_logger()

router = APIRouter()


@router.get("/api/brains")
async def get_brains_status() -> Dict[str, Any]:
    """Real-time rate limit status for all brains with exact reset times."""
    import time as _time
    try:
        from ...infra.rate_monitor import BRAIN_LIMITS, RateMonitor
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


@router.get("/api/cortex")
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


@router.get("/api/learnings")
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


@router.get("/api/router")
async def get_router_status(brain_router: Any = None, rate_monitor: Any = None) -> Dict[str, Any]:
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
        from ...infra.rate_monitor import RateMonitor
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


@router.post("/api/chat")
async def chat_with_brain(
    request: Request,
    brain_router: Any = None,
    rate_monitor: Any = None,
) -> Dict[str, Any]:
    """Send a message through the brain router. Returns response + metadata."""
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

    router_obj = brain_router
    if not router_obj:
        return {"ok": False, "error": "Brain router not available"}

    t0 = _time.time()
    try:
        # Pick brain
        if brain_name:
            brain = router_obj.get_brain(brain_name)
        else:
            # Auto-route via smart_route (rate-aware)
            auto_name, _ = router_obj.smart_route(message, rate_monitor=rate_monitor)
            brain = router_obj.get_brain(auto_name) if auto_name != "zero-token" else router_obj.get_brain("gemini")
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
