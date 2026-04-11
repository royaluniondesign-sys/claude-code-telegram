"""N8N client — calls N8N webhooks on RUD server.

N8N URL from env: RUD_N8N_URL (default http://192.168.1.219:5678)
"""
from __future__ import annotations

import os
from typing import Any

import structlog

logger = structlog.get_logger()

_DEFAULT_N8N_URL = "http://192.168.1.219:5678"


def _n8n_base_url() -> str:
    """Return N8N base URL from env or default."""
    return os.environ.get("RUD_N8N_URL", _DEFAULT_N8N_URL).rstrip("/")


async def call_webhook(
    webhook_path: str,
    payload: dict[str, Any],
    timeout: int = 30,
) -> dict[str, Any]:
    """POST to {RUD_N8N_URL}/webhook/{webhook_path}.

    Returns parsed JSON response or {"error": str} on failure.
    """
    import aiohttp

    base = _n8n_base_url()
    url = f"{base}/webhook/{webhook_path.lstrip('/')}"

    logger.info("n8n_webhook_call", url=url, payload_keys=list(payload.keys()))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.warning("n8n_webhook_error", status=resp.status, body=body[:300])
                    return {"error": f"HTTP {resp.status}: {body[:200]}"}
                try:
                    import json as _json
                    return _json.loads(body) if body.strip() else {"ok": True}
                except Exception:
                    return {"raw": body[:500]}
    except aiohttp.ClientConnectorError as e:
        logger.error("n8n_unreachable", url=url, error=str(e))
        return {"error": f"N8N unreachable at {base}. Set RUD_N8N_URL env var."}
    except Exception as e:
        logger.error("n8n_webhook_exception", error=str(e))
        return {"error": str(e)}


async def trigger_social_post(
    platform: str,
    images_b64: list[str],
    captions: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Call /webhook/aura-social-post with structured payload N8N can parse.

    Payload structure:
      - platform: "instagram" | "twitter" | "linkedin"
      - post_type: "carousel" | "post" | "thread"
      - images: list of base64-encoded image strings
      - captions: list of caption strings (one per image for carousel)
      - metadata: dict with topic, style, count, timestamp
    """
    payload: dict[str, Any] = {
        "platform": platform,
        "post_type": metadata.get("post_type", "post"),
        "images": images_b64,
        "captions": captions,
        "metadata": metadata,
    }
    return await call_webhook("aura-social-post", payload)


async def check_n8n_health() -> bool:
    """GET {RUD_N8N_URL}/healthz — returns True if N8N is up."""
    import aiohttp

    base = _n8n_base_url()
    url = f"{base}/healthz"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status < 400
    except Exception:
        return False
