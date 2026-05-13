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
        self._app.router.add_get("/", self._handle_ui)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_post("/start", self._handle_start)
        self._app.router.add_post("/stop", self._handle_stop)
        self._app.router.add_post("/send", self._handle_send)
        self._app.router.add_get("/transcript", self._handle_transcript)
        self._app.router.add_post("/sleep", self._handle_sleep)
        self._app.router.add_post("/wake", self._handle_wake)

    # ── HTTP handlers ─────────────────────────────────────────────────────────

    async def _handle_ui(self, request: web.Request) -> web.Response:
        """Serve the AURA Voice web dashboard."""
        html = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>✨ AURA Voice</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  :root{--bg:#0d0d0d;--panel:#141414;--border:#1e1e1e;--pri:#a78bfa;--acc:#f59e0b;--green:#22c55e;--red:#ef4444;--text:#e5e5e5;--dim:#6b7280;--font:'Menlo','Monaco','Consolas',monospace}
  body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;display:flex;flex-direction:column}
  header{background:var(--panel);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:12px}
  #dot{width:10px;height:10px;border-radius:50%;background:var(--red);transition:.3s}
  #dot.live{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
  h1{font-size:18px;letter-spacing:4px;color:var(--pri)}
  #model-lbl{font-size:10px;color:var(--dim);margin-left:auto}
  .grid{display:grid;grid-template-columns:220px 1fr 220px;gap:1px;flex:1;background:var(--border)}
  .panel{background:var(--panel);padding:14px;overflow:hidden;display:flex;flex-direction:column;gap:10px}
  .sec-title{font-size:9px;letter-spacing:2px;color:var(--dim);text-transform:uppercase}
  .metric{display:flex;flex-direction:column;gap:4px}
  .bar-bg{height:4px;background:#1e1e1e;border-radius:2px}
  .bar-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--pri),var(--acc));transition:.5s}
  .bar-lbl{font-size:10px;color:var(--dim);display:flex;justify-content:space-between}
  #transcript{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:4px 0}
  #transcript::-webkit-scrollbar{width:4px}
  #transcript::-webkit-scrollbar-track{background:transparent}
  #transcript::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
  .msg{font-size:12px;line-height:1.5;padding:6px 10px;border-radius:6px;word-break:break-word}
  .msg.aura{background:#1e1035;border-left:3px solid var(--pri)}
  .msg.user{background:#1a1a1a;border-left:3px solid var(--dim)}
  .msg.sys{background:#0f1a0f;border-left:3px solid var(--green);font-size:10px;color:var(--dim)}
  .speaker{font-size:9px;letter-spacing:1px;margin-bottom:2px}
  .aura .speaker{color:var(--pri)}
  .user .speaker{color:var(--dim)}
  .sys .speaker{color:var(--green)}
  #send-row{display:flex;gap:6px}
  #send-input{flex:1;background:#1a1a1a;border:1px solid var(--border);color:var(--text);font-family:var(--font);font-size:12px;padding:8px 10px;border-radius:6px;outline:none}
  #send-input:focus{border-color:var(--pri)}
  button{background:var(--pri);color:#000;border:none;font-family:var(--font);font-size:11px;font-weight:bold;padding:8px 14px;border-radius:6px;cursor:pointer;letter-spacing:1px;transition:.2s}
  button:hover{opacity:.85}
  button.danger{background:var(--red)}
  button.stop-btn{background:#2a2a2a;color:var(--text)}
  #tools-log{flex:1;overflow-y:auto;font-size:10px;display:flex;flex-direction:column;gap:4px}
  .tool-entry{background:#1a1a1a;padding:5px 8px;border-radius:4px;color:var(--acc);border-left:2px solid var(--acc)}
  #uptime{font-size:11px;color:var(--dim)}
  #status-text{font-size:13px;font-weight:bold}
  #status-text.running{color:var(--green)}
  #status-text.stopped{color:var(--red)}
  .ctrl-row{display:flex;gap:6px}
  footer{background:var(--panel);border-top:1px solid var(--border);padding:8px 20px;font-size:9px;color:var(--dim);letter-spacing:1px;text-align:center}
  #wave{height:40px;width:100%}
</style>
</head>
<body>
<header>
  <div id="dot"></div>
  <h1>✨ A U R A</h1>
  <span id="model-lbl">Gemini 2.5 Flash Native Audio</span>
</header>
<div class="grid">
  <!-- Left panel: metrics -->
  <div class="panel">
    <div class="sec-title">Estado</div>
    <div id="status-text" class="stopped">Offline</div>
    <div id="uptime">Uptime: —</div>
    <div class="sec-title" style="margin-top:8px">Sistema</div>
    <div class="metric">
      <div class="bar-lbl"><span>CPU</span><span id="cpu-pct">—</span></div>
      <div class="bar-bg"><div class="bar-fill" id="cpu-bar" style="width:0%"></div></div>
    </div>
    <div class="metric">
      <div class="bar-lbl"><span>RAM</span><span id="ram-pct">—</span></div>
      <div class="bar-bg"><div class="bar-fill" id="ram-bar" style="width:0%"></div></div>
    </div>
    <div class="metric">
      <div class="bar-lbl"><span>Turns</span><span id="turns">0</span></div>
    </div>
    <div style="flex:1"></div>
    <div class="ctrl-row">
      <button id="btn-start" onclick="startAgent()">▶ START</button>
      <button id="btn-stop" class="stop-btn" onclick="stopAgent()">⏹ STOP</button>
    </div>
  </div>

  <!-- Center: transcript -->
  <div class="panel">
    <div class="sec-title">Transcripción en vivo</div>
    <canvas id="wave"></canvas>
    <div id="transcript"></div>
    <div id="send-row">
      <input id="send-input" placeholder="Escribe un mensaje a AURA..." onkeydown="if(event.key==='Enter')sendText()">
      <button onclick="sendText()">↑ SEND</button>
    </div>
  </div>

  <!-- Right panel: tools -->
  <div class="panel">
    <div class="sec-title">Tool calls</div>
    <div id="tools-log"></div>
  </div>
</div>
<footer>AURA v2 · Gemini 2.5 Flash Native Audio · 52 tools · AURA + Hermes + Mac · localhost:8085</footer>

<script>
let seenCount = 0;
let animFrame = 0;

// Waveform animation
const canvas = document.getElementById('wave');
const ctx = canvas.getContext('2d');
let isLive = false;
function drawWave() {
  canvas.width = canvas.offsetWidth;
  canvas.height = canvas.offsetHeight;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const w = canvas.width, h = canvas.height, mid = h/2;
  ctx.strokeStyle = isLive ? '#a78bfa' : '#2a2a2a';
  ctx.lineWidth = 2;
  ctx.beginPath();
  const pts = 80;
  for(let i=0;i<=pts;i++){
    const x = (i/pts)*w;
    const amp = isLive ? (8 + Math.random()*12) : 2;
    const y = mid + Math.sin((i/pts)*Math.PI*8 + animFrame/10)*amp;
    i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
  }
  ctx.stroke();
  animFrame++;
  requestAnimationFrame(drawWave);
}
drawWave();

async function poll() {
  try {
    const [s, t] = await Promise.all([
      fetch('/status').then(r=>r.json()),
      fetch('/transcript?limit=100').then(r=>r.json())
    ]);

    const running = s.status === 'running';
    isLive = running;
    document.getElementById('dot').className = running ? 'live' : '';
    const stEl = document.getElementById('status-text');
    stEl.textContent = running ? 'Running' : s.status.toUpperCase();
    stEl.className = running ? 'running' : 'stopped';
    document.getElementById('uptime').textContent = 'Uptime: ' + (s.uptime_s || 0) + 's';
    document.getElementById('turns').textContent = s.transcript_count || 0;
    document.getElementById('model-lbl').textContent = s.model || 'Gemini 2.5 Flash Native Audio';

    // CPU / RAM via psutil (approximate via navigator)
    if(window.performance && window.performance.memory){
      const used = window.performance.memory.usedJSHeapSize;
      const total = window.performance.memory.totalJSHeapSize;
      const pct = Math.round((used/total)*100);
      document.getElementById('ram-bar').style.width = pct+'%';
      document.getElementById('ram-pct').textContent = pct+'%';
    }

    // Transcript
    const entries = t.transcript || [];
    const box = document.getElementById('transcript');
    if(entries.length > seenCount){
      const newOnes = entries.slice(seenCount);
      newOnes.forEach(e => {
        const div = document.createElement('div');
        div.className = 'msg ' + (e.speaker === 'aura' ? 'aura' : 'user');
        const icon = e.speaker === 'aura' ? '✨' : '🎤';
        div.innerHTML = '<div class="speaker">' + icon + ' ' + e.speaker.toUpperCase() + '</div>' + escHtml(e.text);
        box.appendChild(div);
      });
      seenCount = entries.length;
      box.scrollTop = box.scrollHeight;
    }
  } catch(e) {
    document.getElementById('status-text').textContent = 'OFFLINE';
    document.getElementById('dot').className = '';
  }
}

function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }

async function sendText(){
  const inp = document.getElementById('send-input');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  addSysMsg('💬 Enviado: ' + text);
  await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
}

async function startAgent(){
  addSysMsg('▶ Iniciando voice agent...');
  await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
}

async function stopAgent(){
  addSysMsg('⏹ Deteniendo...');
  await fetch('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
}

function addSysMsg(text){
  const box = document.getElementById('transcript');
  const div = document.createElement('div');
  div.className = 'msg sys';
  div.innerHTML = '<div class="speaker">⚙ SYSTEM</div>' + escHtml(text);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// Poll every 800ms
poll();
setInterval(poll, 800);
</script>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_status(self, request: web.Request) -> web.Response:
        ready = self._agent is not None and self._agent.is_ready()
        sleeping = self._agent.is_sleeping() if self._agent else False
        return web.json_response({
            "status": "sleeping" if (ready and sleeping) else ("running" if ready else ("starting" if self._agent else "stopped")),
            "sleeping": sleeping,
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

    async def _handle_sleep(self, request: web.Request) -> web.Response:
        if not self._agent or not self._agent.is_ready():
            return web.json_response({"ok": False, "error": "Agent not running"}, status=503)
        self._agent.sleep()
        return web.json_response({"ok": True, "sleeping": True})

    async def _handle_wake(self, request: web.Request) -> web.Response:
        if not self._agent or not self._agent.is_ready():
            return web.json_response({"ok": False, "error": "Agent not running"}, status=503)
        self._agent.wake()
        return web.json_response({"ok": True, "sleeping": False})

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
        # Preserve sleeping state across restarts
        was_sleeping = self._agent.is_sleeping() if self._agent else False
        self._agent = create_agent(
            on_transcript=self._on_transcript,
            on_tool_call=self._on_tool_call,
        )
        self._agent.start(timeout=30)
        if was_sleeping:
            self._agent.sleep(announce=False)  # restore silently
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

        # Watchdog: restart agent if it dies (every 15s check)
        asyncio.create_task(self._watchdog())

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            _STATE_FILE.unlink(missing_ok=True)
            await runner.cleanup()

    async def _watchdog(self) -> None:
        """Restart voice agent automatically if it dies."""
        await asyncio.sleep(20)  # initial grace period
        while True:
            await asyncio.sleep(15)
            if self._agent and not self._agent.is_ready():
                logger.warning("voice_watchdog_restart")
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._start_agent)
                    logger.info("voice_watchdog_restarted")
                except Exception as e:
                    logger.error("voice_watchdog_restart_failed", error=str(e))


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
