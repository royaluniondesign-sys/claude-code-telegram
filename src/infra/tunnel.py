"""Dashboard tunnel manager — reads URL from ngrok (already running).

ngrok is started separately by the user and exposes port 8080.
This module reads the public URL from ngrok's local API (localhost:4040)
and keeps it up to date, refreshing every 60 seconds.

No cloudflared, no trycloudflare.com — ngrok only.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_DASHBOARD_URL_FILE = Path.home() / ".aura" / "dashboard_url.txt"
_NGROK_API = "http://localhost:4040/api/tunnels"
_DASHBOARD_PORT = 8080
_POLL_INTERVAL = 60  # seconds between ngrok API checks

# Module-level state
_current_url: Optional[str] = None


def get_dashboard_url() -> Optional[str]:
    """Return the current public dashboard URL from ngrok, or None."""
    return _current_url


def _fetch_ngrok_url(port: int = _DASHBOARD_PORT) -> Optional[str]:
    """Query ngrok local API and return the public URL for the given port."""
    try:
        with urllib.request.urlopen(_NGROK_API, timeout=3) as r:
            data = json.loads(r.read())
        for t in data.get("tunnels", []):
            addr = t.get("config", {}).get("addr", "")
            pub = t.get("public_url", "")
            if str(port) in addr and pub.startswith("https://"):
                return pub
    except Exception:
        pass
    return None


def _store_url(url: str) -> None:
    global _current_url
    _current_url = url
    try:
        _DASHBOARD_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DASHBOARD_URL_FILE.write_text(url + "\n", encoding="utf-8")
    except Exception:
        pass


def _clear_url() -> None:
    global _current_url
    _current_url = None
    try:
        _DASHBOARD_URL_FILE.unlink(missing_ok=True)
    except Exception:
        pass


async def _poll_ngrok(port: int) -> None:
    """Background loop: poll ngrok API every 60s and keep URL fresh."""
    while True:
        url = _fetch_ngrok_url(port)
        if url:
            if url != _current_url:
                _store_url(url)
                logger.info("tunnel_url_ready", url=url, port=port)
        else:
            if _current_url:
                logger.warning("tunnel_ngrok_url_lost", previous=_current_url)
            _clear_url()

        try:
            await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            return


async def start_dashboard_tunnel(port: int = _DASHBOARD_PORT) -> asyncio.Task:  # type: ignore[type-arg]
    """Start the ngrok polling task. Returns the asyncio Task."""
    # Do an immediate check so the URL is available right away
    url = _fetch_ngrok_url(port)
    if url:
        _store_url(url)
        logger.info("tunnel_url_ready", url=url, port=port)
    else:
        logger.warning("tunnel_ngrok_not_found", port=port,
                       hint="Start ngrok with: ngrok http 8080")

    task: asyncio.Task = asyncio.create_task(  # type: ignore[type-arg]
        _poll_ngrok(port), name="dashboard-tunnel"
    )
    return task


async def stop_dashboard_tunnel() -> None:
    """Nothing to stop — ngrok is managed externally."""
    _clear_url()
