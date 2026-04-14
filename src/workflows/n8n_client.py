"""N8N client — manages N8N session, flows, and executions for AURA.

Supports both local (Docker, localhost:5678) and remote (RUD server) N8N.
Uses session-cookie auth — no API key or manual setup required.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import aiohttp
import structlog

logger = structlog.get_logger()

_LOCAL_N8N = "http://localhost:5678"
_N8N_EMAIL = "royaluniondesign@gmail.com"
_N8N_PASSWORD = "AuraRUD2026!"
_SOCIAL_FLOW_ID = "gQvwEydQ4Bcj5pru"

# In-memory session cookie cache
_session_cookie: Optional[str] = None


def _n8n_base_url() -> str:
    return os.environ.get("RUD_N8N_URL", _LOCAL_N8N).rstrip("/")


async def _get_n8n_session(base_url: str) -> Optional[str]:
    """Login to N8N and cache session cookie."""
    global _session_cookie
    if _session_cookie:
        return _session_cookie
    try:
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            async with session.post(
                f"{base_url}/rest/login",
                json={"emailOrLdapLoginId": _N8N_EMAIL, "password": _N8N_PASSWORD},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    cookies = "; ".join(f"{c.key}={c.value}" for c in jar)
                    _session_cookie = cookies
                    logger.info("n8n_login_ok", url=base_url)
                    return cookies
    except Exception as e:
        logger.warning("n8n_login_failed", error=str(e))
    return None


async def _n8n_rest(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    base_url: Optional[str] = None,
) -> dict[str, Any]:
    """Make authenticated N8N REST request."""
    global _session_cookie
    base = base_url or _n8n_base_url()
    cookie = await _get_n8n_session(base)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie

    url = f"{base}/rest/{path.lstrip('/')}"
    try:
        async with aiohttp.ClientSession() as session:
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=30),
            }
            if payload is not None:
                kwargs["json"] = payload
            async with getattr(session, method.lower())(url, **kwargs) as resp:
                if resp.status == 401:
                    _session_cookie = None
                    return {"error": "unauthorized", "status": 401}
                text = await resp.text()
                try:
                    return json.loads(text)
                except Exception:
                    return {"error": f"non-json: {text[:100]}"}
    except Exception as e:
        return {"error": str(e)}


async def execute_social_flow(post_data: dict[str, Any]) -> dict[str, Any]:
    """Execute the AURA Social Post N8N flow directly.

    post_data: {platform, caption, image_url, topic}
    Returns: {ok, post_url, message, mock}
    """
    import asyncio

    base = _n8n_base_url()
    flow_id = os.environ.get("N8N_SOCIAL_FLOW_ID", _SOCIAL_FLOW_ID)

    resp = await _n8n_rest(
        "POST",
        f"workflows/{flow_id}/run",
        payload={
            "startNodes": [],
            "destinationNode": "",
            "pinData": {"Start": [{"json": post_data}]},
        },
        base_url=base,
    )

    exec_id = resp.get("data", {}).get("executionId")
    if not exec_id:
        return {"ok": False, "error": f"N8N run failed: {resp.get('message', str(resp))[:80]}"}

    for _ in range(15):
        await asyncio.sleep(1)
        ex_resp = await _n8n_rest("GET", f"executions/{exec_id}", base_url=base)
        ex = ex_resp.get("data", ex_resp)
        status = ex.get("status", "")
        if status in ("success", "error", "crashed"):
            if status == "success":
                try:
                    run_data = (
                        ex.get("data", {})
                        .get("resultData", {})
                        .get("runData", {})
                    )
                    for node_name in ["Success Response", "Mock Response", "Respond"]:
                        if node_name in run_data:
                            items = run_data[node_name]
                            if items:
                                out = items[0].get("data", {}).get("main", [[]])[0]
                                if out:
                                    result = out[0].get("json", {})
                                    return {
                                        "ok": result.get("ok", False),
                                        "post_url": result.get("post_url", ""),
                                        "message": result.get("message", ""),
                                        "mock": result.get("mock", False),
                                    }
                except Exception:
                    pass
                return {"ok": True, "post_url": "", "message": "Executed"}
            return {"ok": False, "error": f"Execution {status}"}

    return {"ok": False, "error": "Execution timeout (>15s)"}


async def call_webhook(
    webhook_path: str,
    payload: dict[str, Any],
    timeout: int = 30,
) -> dict[str, Any]:
    """POST to {N8N_URL}/webhook/{webhook_path}.

    Falls back to direct flow execution if webhook not active.
    """
    base = _n8n_base_url()
    url = f"{base}/webhook/{webhook_path.lstrip('/')}"

    logger.info("n8n_webhook_call", url=url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 404:
                    logger.info("n8n_webhook_fallback_to_execute")
                    if "social" in webhook_path:
                        return await execute_social_flow(payload)
                    return {"error": f"Webhook '{webhook_path}' not registered"}
                text = await resp.text()
                try:
                    return json.loads(text)
                except Exception:
                    return {"result": text[:500]}
    except aiohttp.ClientConnectorError as e:
        logger.error("n8n_unreachable", url=url, error=str(e))
        return {"error": f"N8N unreachable at {base}"}
    except Exception as e:
        logger.error("n8n_webhook_exception", error=str(e))
        return {"error": str(e)}


async def trigger_social_post(
    platform: str,
    images_b64: list[str],
    captions: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Trigger social post via N8N (webhook or direct execution)."""
    payload: dict[str, Any] = {
        "platform": platform,
        "post_type": metadata.get("post_type", "post"),
        "images": [{"b64": b} for b in images_b64],
        "captions": captions,
        "caption": captions[0] if captions else "",
        "metadata": metadata,
        "topic": metadata.get("topic", ""),
    }
    # Try webhook first, falls back to direct execution automatically
    return await call_webhook("aura-social", payload)


async def check_n8n_health() -> bool:
    """Check if N8N is reachable."""
    base = _n8n_base_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/healthz",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status < 400
    except Exception:
        return False


async def list_workflows() -> list[dict[str, Any]]:
    """List all N8N workflows."""
    resp = await _n8n_rest("GET", "workflows")
    return resp.get("data", [])
