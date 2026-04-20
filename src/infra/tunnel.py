"""Dashboard tunnel manager — cloudflared quick tunnel.

El wrapper /usr/local/bin/aura-dashboard-tunnel inicia cloudflared,
captura la URL de trycloudflare.com y la escribe en ~/.aura/dashboard_url.txt.

Este módulo lee ese archivo periódicamente y expone get_dashboard_url().
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_DASHBOARD_URL_FILE = Path.home() / ".aura" / "dashboard_url.txt"
_POLL_INTERVAL = 30  # segundos entre lecturas del archivo

# Estado en memoria
_current_url: Optional[str] = None


def get_dashboard_url() -> Optional[str]:
    """Retorna la URL pública actual del dashboard, o None si no hay túnel."""
    return _current_url


def _read_url_file() -> Optional[str]:
    """Lee la URL del archivo escrito por el wrapper de cloudflared."""
    try:
        if _DASHBOARD_URL_FILE.exists():
            url = _DASHBOARD_URL_FILE.read_text(encoding="utf-8").strip()
            if url.startswith("https://"):
                return url
    except Exception:
        pass
    return None


def _store_url(url: str) -> None:
    global _current_url
    if url != _current_url:
        _current_url = url
        logger.info("tunnel_url_ready", url=url)


def _clear_url() -> None:
    global _current_url
    if _current_url:
        logger.warning("tunnel_url_lost", previous=_current_url)
    _current_url = None


async def _poll_file() -> None:
    """Background loop: relee el archivo cada _POLL_INTERVAL segundos."""
    while True:
        url = _read_url_file()
        if url:
            _store_url(url)
        else:
            _clear_url()
        try:
            await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            return


async def start_dashboard_tunnel(port: int = 8080) -> asyncio.Task:  # type: ignore[type-arg]
    """Inicia el poller del archivo de URL. Retorna el asyncio Task."""
    # Lectura inmediata para tener URL disponible desde el arranque
    url = _read_url_file()
    if url:
        _store_url(url)
    else:
        logger.warning("tunnel_url_not_found_yet",
                       hint="cloudflared wrapper should write to ~/.aura/dashboard_url.txt")

    task: asyncio.Task = asyncio.create_task(  # type: ignore[type-arg]
        _poll_file(), name="dashboard-tunnel"
    )
    return task


async def stop_dashboard_tunnel() -> None:
    """Limpia estado en memoria."""
    _clear_url()
