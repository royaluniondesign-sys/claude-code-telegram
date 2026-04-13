"""Dashboard tunnel manager — exposes port 8080 via cloudflared quick tunnel.

Starts a cloudflared quick tunnel in a background subprocess, parses the
public URL from stdout, stores it to ~/.aura/dashboard_url.txt, and exposes
it via a module-level getter so the API layer can serve /api/dashboard-url.

No account or credentials required — cloudflared quick tunnels are free and
anonymous (trycloudflare.com subdomain, no uptime guarantee).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_DASHBOARD_URL_FILE = Path.home() / ".aura" / "dashboard_url.txt"
_URL_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")

# Module-level state — mutated only by _run_tunnel()
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


async def _run_tunnel(port: int) -> None:
    """Launch cloudflared quick tunnel and keep it alive."""
    global _tunnel_proc

    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        logger.warning("tunnel_cloudflared_missing", reason="cloudflared not in PATH — dashboard will be local only")
        return

    cmd = [
        cloudflared, "tunnel",
        "--url", f"http://localhost:{port}",
        "--no-autoupdate",
    ]

    while True:
        try:
            logger.info("tunnel_starting", cmd=" ".join(cmd))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merge stderr → stdout so we can read it
                env={**os.environ, "TERM": "dumb"},
            )
            _tunnel_proc = proc
            _clear_url()

            # Read output line by line to detect the public URL
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

                m = _URL_RE.search(line)
                if m:
                    url = m.group(0)
                    _store_url(url)
                    logger.info("tunnel_url_ready", url=url, port=port)

            await proc.wait()
            code = proc.returncode
            logger.warning("tunnel_exited", code=code)
            _clear_url()

        except asyncio.CancelledError:
            logger.info("tunnel_cancelled")
            if _tunnel_proc and _tunnel_proc.returncode is None:
                _tunnel_proc.terminate()
            _clear_url()
            return
        except Exception as e:
            logger.error("tunnel_error", error=str(e))
            _clear_url()

        # Backoff before restart
        logger.info("tunnel_restarting_in", seconds=15)
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            return


async def start_dashboard_tunnel(port: int = 8080) -> asyncio.Task:  # type: ignore[type-arg]
    """Start the tunnel as a background asyncio task. Returns the task."""
    task: asyncio.Task = asyncio.create_task(_run_tunnel(port), name="dashboard-tunnel")  # type: ignore[type-arg]
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
