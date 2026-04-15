"""Dashboard tunnel manager — exposes the dashboard port to the public internet.

Supports two backends, selected automatically at startup:

1. **ngrok** (preferred when ``NGROK_AUTHTOKEN`` is set in the environment)
   Uses the authenticated account of the caller, which means:
     * stable session lifetime (no random URL roulette on every restart)
     * free-tier subdomain on ``*.ngrok-free.app``
     * uses a **separate account** from Termora's ngrok, so there is no
       concurrent-session collision (each token = one active agent).

2. **cloudflared quick tunnel** (fallback when no token is provided)
   Anonymous ``*.trycloudflare.com`` URL, ephemeral, survives as long as the
   subprocess runs.

Public API is identical regardless of backend:
    - ``start_dashboard_tunnel(port)`` -> asyncio.Task
    - ``stop_dashboard_tunnel()``
    - ``get_dashboard_url()`` -> Optional[str]

The current public URL is also persisted to ``~/.aura/dashboard_url.txt`` so
external consumers (CLI commands, Telegram bot handlers) can read it without
needing an API round-trip.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

_DASHBOARD_URL_FILE = Path.home() / ".aura" / "dashboard_url.txt"
_CLOUDFLARED_URL_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
_NGROK_URL_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.ngrok(?:-free)?\.(?:app|io|dev)")

# ngrok local web UI — default 4040 collides with Termora's ngrok on the same
# machine, so we move it out of the way. Anything unused is fine.
_NGROK_WEB_ADDR = os.environ.get("NGROK_WEB_ADDR", "localhost:4041")

# Module-level state — mutated only by the active backend loop.
_current_url: Optional[str] = None
_tunnel_proc: Optional[asyncio.subprocess.Process] = None


def get_dashboard_url() -> Optional[str]:
    """Return the current public dashboard URL, or None if tunnel is down."""
    return _current_url


def _store_url(url: str) -> None:
    """Persist URL to disk for external consumers."""
    global _current_url
    _current_url = url
    try:
        _DASHBOARD_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DASHBOARD_URL_FILE.write_text(url + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("tunnel_url_persist_failed", error=str(e))


def _clear_url() -> None:
    global _current_url
    _current_url = None
    try:
        if _DASHBOARD_URL_FILE.exists():
            _DASHBOARD_URL_FILE.unlink()
    except Exception:
        pass


def _extract_ngrok_url(line: str) -> Optional[str]:
    """Pull the public URL out of an ngrok JSON log line (or plain text)."""
    line = line.strip()
    if not line:
        return None
    # ngrok v3 emits JSON when --log-format=json; try JSON first, regex fallback.
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            url = obj.get("url") or ""
            if isinstance(url, str) and url.startswith("https://") and "ngrok" in url:
                return url
        except Exception:
            pass
    m = _NGROK_URL_RE.search(line)
    return m.group(0) if m else None


async def _run_ngrok(port: int, authtoken: str) -> None:
    """Launch ngrok HTTP tunnel against ``port`` and keep it alive."""
    global _tunnel_proc

    ngrok_bin = shutil.which("ngrok")
    if not ngrok_bin:
        logger.warning(
            "tunnel_ngrok_missing",
            reason="ngrok binary not in PATH — install from https://ngrok.com/download",
        )
        return

    cmd: List[str] = [
        ngrok_bin, "http", str(port),
        "--log=stdout",
        "--log-format=json",
        "--log-level=info",
        f"--web-addr={_NGROK_WEB_ADDR}",
    ]

    # Keep parent env but inject/override the authtoken. Also pass TERM=dumb to
    # suppress any interactive UI attempts.
    child_env = {**os.environ, "NGROK_AUTHTOKEN": authtoken, "TERM": "dumb"}

    while True:
        try:
            logger.info(
                "tunnel_starting",
                backend="ngrok",
                port=port,
                web_addr=_NGROK_WEB_ADDR,
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=child_env,
            )
            _tunnel_proc = proc
            _clear_url()

            assert proc.stdout is not None
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue

                if not raw:
                    break  # EOF

                line = raw.decode(errors="replace").rstrip()
                logger.debug("ngrok", line=line[:300])

                url = _extract_ngrok_url(line)
                if url and url != _current_url:
                    _store_url(url)
                    logger.info("tunnel_url_ready", backend="ngrok", url=url, port=port)

            await proc.wait()
            logger.warning("tunnel_exited", backend="ngrok", code=proc.returncode)
            _clear_url()

        except asyncio.CancelledError:
            logger.info("tunnel_cancelled", backend="ngrok")
            if _tunnel_proc and _tunnel_proc.returncode is None:
                _tunnel_proc.terminate()
            _clear_url()
            return
        except Exception as e:
            logger.error("tunnel_error", backend="ngrok", error=str(e))
            _clear_url()

        logger.info("tunnel_restarting_in", backend="ngrok", seconds=15)
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            return


async def _run_cloudflared(port: int) -> None:
    """Launch cloudflared quick tunnel and keep it alive."""
    global _tunnel_proc

    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        logger.warning(
            "tunnel_cloudflared_missing",
            reason="cloudflared not in PATH — dashboard will be local only",
        )
        return

    cmd: List[str] = [
        cloudflared, "tunnel",
        "--url", f"http://localhost:{port}",
        "--no-autoupdate",
    ]

    while True:
        try:
            logger.info("tunnel_starting", backend="cloudflared", cmd=" ".join(cmd))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "TERM": "dumb"},
            )
            _tunnel_proc = proc
            _clear_url()

            assert proc.stdout is not None
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue

                if not raw:
                    break  # EOF

                line = raw.decode(errors="replace").rstrip()
                logger.debug("cloudflared", line=line[:200])

                m = _CLOUDFLARED_URL_RE.search(line)
                if m:
                    url = m.group(0)
                    _store_url(url)
                    logger.info(
                        "tunnel_url_ready", backend="cloudflared", url=url, port=port
                    )

            await proc.wait()
            logger.warning("tunnel_exited", backend="cloudflared", code=proc.returncode)
            _clear_url()

        except asyncio.CancelledError:
            logger.info("tunnel_cancelled", backend="cloudflared")
            if _tunnel_proc and _tunnel_proc.returncode is None:
                _tunnel_proc.terminate()
            _clear_url()
            return
        except Exception as e:
            logger.error("tunnel_error", backend="cloudflared", error=str(e))
            _clear_url()

        logger.info("tunnel_restarting_in", backend="cloudflared", seconds=15)
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            return


async def _run_tunnel(port: int) -> None:
    """Dispatch to the configured backend based on environment.

    If ``NGROK_AUTHTOKEN`` is set, use ngrok. Otherwise, fall back to the
    anonymous cloudflared quick tunnel so existing deployments that don't
    have a token keep working unchanged.
    """
    authtoken = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if authtoken:
        await _run_ngrok(port, authtoken)
    else:
        await _run_cloudflared(port)


async def start_dashboard_tunnel(port: int = 8080) -> asyncio.Task:  # type: ignore[type-arg]
    """Start the tunnel as a background asyncio task. Returns the task."""
    task: asyncio.Task = asyncio.create_task(  # type: ignore[type-arg]
        _run_tunnel(port), name="dashboard-tunnel"
    )
    return task


async def stop_dashboard_tunnel() -> None:
    """Terminate the tunnel subprocess if running."""
    global _tunnel_proc
    if _tunnel_proc and _tunnel_proc.returncode is None:
        try:
            _tunnel_proc.terminate()
            await asyncio.wait_for(_tunnel_proc.wait(), timeout=5)
        except Exception:
            pass
    _clear_url()
