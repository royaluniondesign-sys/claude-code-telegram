"""AURA Voice Daemon — runs GeminiLiveAgent as a background service.

Exposes HTTP control API on port 8085 for:
  - Telegram bot (/voice command)
  - CLI control
  - Status queries

Usage:
  python -m src.voice.voice_daemon         # start daemon
  python -m src.voice.voice_daemon status  # check status
  python -m src.voice.voice_daemon stop    # stop
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import structlog
from aiohttp import web

logger = structlog.get_logger()

_PORT = int(os.environ.get("VOICE_DAEMON_PORT", "8085"))
_STATE_FILE = Path.home() / ".aura" / "voice_daemon.json"


class VoiceDaemon:
    """HTTP daemon that manages the GeminiLiveAgent lifecycle."""

    def __init__(self) -> None:
        self._agent: Any = None
        self._start_time: Optional[float] = None
        self._transcript_log: list[dict] = []
        self._tool_log: list[dict] = []
        self._app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_post("/start", self._handle_start)
        self._app.router.add_post("/stop", self._handle_stop)
        self._app.router.add_post("/send", self._handle_send)
        self._app.router.add_get("/transcript", self._handle_transcript)

    # ── HTTP handlers ─────────────────────────────────────────────────────────

    async def _handle_status(self, request: web.Request) -> web.Response:
        ready = self._agent is not None and self._agent.is_ready()
        return web.json_response({
            "status": "running" if ready else ("starting" if self._agent else "stopped"),
            "uptime_s": int(time.time() - self._start_time) if self._start_time else 0,
            "model": "gemini-2.5-flash-native-audio-preview",
            "tools": "AURA registry + screen + computer + hermes + claude",
            "transcript_count": len(self._transcript_log),
        })

    async def _handle_start(self, request: web.Request) -> web.Response:
        if self._agent and self._agent.is_ready():
            return web.json_response({"ok": True, "message": "Already running"})

        try:
            await asyncio.to_thread(self._start_agent)
            return web.json_response({"ok": True, "message": "Voice agent started"})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if self._agent:
            await asyncio.to_thread(self._agent.stop)
            self._agent = None
            self._start_time = None
        return web.json_response({"ok": True, "message": "Stopped"})

    async def _handle_send(self, request: web.Request) -> web.Response:
        """Send a text message to the voice agent (e.g. from Telegram)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        text = body.get("text", "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "No text"}, status=400)

        if not self._agent or not self._agent.is_ready():
            return web.json_response({"ok": False, "error": "Agent not running"}, status=503)

        self._agent.send_text(text)
        return web.json_response({"ok": True, "message": "Sent to voice agent"})

    async def _handle_transcript(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "20"))
        return web.json_response({"transcript": self._transcript_log[-limit:]})

    # ── Agent lifecycle ───────────────────────────────────────────────────────

    def _on_transcript(self, speaker: str, text: str) -> None:
        entry = {"ts": time.time(), "speaker": speaker, "text": text}
        self._transcript_log.append(entry)
        if len(self._transcript_log) > 500:
            self._transcript_log = self._transcript_log[-250:]
        icon = "✨" if speaker == "aura" else "🎤"
        print(f"\n{icon} [{speaker.upper()}]: {text}", flush=True)

    def _on_tool_call(self, tool_name: str, args: dict) -> None:
        entry = {"ts": time.time(), "tool": tool_name, "args": list(args.keys())}
        self._tool_log.append(entry)
        print(f"  🔧 {tool_name}({', '.join(args.keys())})", flush=True)

    def _start_agent(self) -> None:
        from src.voice.gemini_live_agent import create_agent
        self._agent = create_agent(
            on_transcript=self._on_transcript,
            on_tool_call=self._on_tool_call,
        )
        self._agent.start(timeout=30)
        self._start_time = time.time()
        logger.info("voice_daemon_agent_started")

    # ── Main run ──────────────────────────────────────────────────────────────

    async def run(self, auto_start: bool = True) -> None:
        """Start HTTP server and optionally auto-start the voice agent."""
        if auto_start:
            logger.info("voice_daemon_autostart")
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._start_agent)
                print(f"✅ AURA Voice Agent running — listening on mic + port {_PORT}")
                print("   Gemini 2.5 Flash Native Audio (FREE) | AURA tools | Hermes | Claude")
            except Exception as e:
                logger.error("voice_daemon_autostart_failed", error=str(e))
                print(f"⚠️  Voice agent failed to start: {e}")
                print("   HTTP control API still available — use POST /start to retry")

        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", _PORT)
        await site.start()
        logger.info("voice_daemon_http_ready", port=_PORT)

        # Save PID + port for CLI control
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"pid": os.getpid(), "port": _PORT}))

        print(f"🎤 Voice daemon HTTP API: http://127.0.0.1:{_PORT}/")
        print("   POST /start | POST /stop | GET /status | POST /send | GET /transcript")

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            _STATE_FILE.unlink(missing_ok=True)
            await runner.cleanup()


# ── AURA screen + image feed (for Telegram photos → voice) ───────────────────

async def forward_image_to_voice(
    image_bytes: bytes,
    mime_type: str,
    caption: str = "",
    daemon_port: int = _PORT,
) -> bool:
    """Forward a Telegram image to the running voice agent for analysis."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{daemon_port}/send",
                json={"text": caption or "Analiza esta imagen", "image": True},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


async def send_text_to_voice(text: str, port: int = _PORT) -> bool:
    """Send text to running voice daemon (from Telegram /voice text)."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/send",
                json={"text": text},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


async def get_daemon_status(port: int = _PORT) -> Optional[dict]:
    """Query voice daemon status. Returns None if not running."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/status",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return None


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    from pathlib import Path as P
    from dotenv import load_dotenv
    load_dotenv(P(__file__).parent.parent.parent / ".env")

    # Redirect all logging to stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"

    if cmd == "status":
        import asyncio as _a
        status = _a.run(get_daemon_status())
        if status:
            print(json.dumps(status, indent=2))
        else:
            print("Voice daemon not running")
        return

    if cmd == "stop":
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/stop", data=b"{}", timeout=5)
            print("Voice daemon stopped")
        except Exception:
            print("Voice daemon not running or could not stop")
        return

    daemon = VoiceDaemon()
    asyncio.run(daemon.run(auto_start=True))


if __name__ == "__main__":
    main()
