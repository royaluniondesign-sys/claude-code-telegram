"""Conductor router: /api/conductor/*, /api/proactive/*, /api/stream/orchestration."""

import asyncio
from typing import Any, Dict

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = structlog.get_logger()

router = APIRouter()


@router.get("/api/stream/orchestration")
async def stream_orchestration() -> StreamingResponse:
    """Server-Sent Events stream of live conductor/orchestration events.

    Events: planning, plan_created, step_started, step_completed,
            step_failed, run_completed, run_failed
    """
    import asyncio as _aio
    import json as _j
    import time as _t
    from ...brains.conductor import orch_subscribe, orch_unsubscribe

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
                from ...infra.proactive_loop import get_proactive_status
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

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# NOTE: /api/conductor/run requires brain_router from the create_api_app() closure
# and is therefore defined inline in server.py.


@router.get("/api/conductor/status")
async def conductor_status() -> Dict[str, Any]:
    """Return whether a conductor is initialized and available."""
    from ...brains.conductor import get_conductor, _subscribers
    c = get_conductor()
    return {
        "available": c is not None,
        "sse_subscribers": len(_subscribers),
        "stream_url": "/api/stream/orchestration",
    }


@router.get("/api/conductor/history")
async def conductor_history_endpoint() -> Dict[str, Any]:
    """Return recent conductor run history for the Sessions panel."""
    try:
        from ...infra.conductor_history import get_history, history_stats
        runs = get_history(limit=50)
        return {"ok": True, "runs": runs, "stats": history_stats()}
    except Exception as e:
        return {"ok": False, "runs": [], "stats": {}, "error": str(e)}


@router.get("/api/conductor/metrics")
async def conductor_metrics_endpoint() -> Dict[str, Any]:
    """Return conductor metrics: success rates by layer and brain, best brain, avg durations."""
    try:
        from ...infra.conductor_history import conductor_metrics
        return {"ok": True, "metrics": conductor_metrics()}
    except Exception as e:
        return {"ok": False, "metrics": {}, "error": str(e)}


@router.get("/api/proactive/status")
async def proactive_status_endpoint() -> Dict[str, Any]:
    """Return proactive loop status: running, last/next run, stats."""
    try:
        from ...infra.proactive_loop import get_proactive_status
        return {"ok": True, **get_proactive_status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
