#!/usr/bin/env python3
"""AURA Menu Bar App — acceso rápido sin abrir Telegram.

Requires: uv add rumps httpx  (already in project deps)
Run:      python3 menubar/aura_bar.py
Auto-start: bash menubar/install.sh
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import rumps

# ── Path setup (so we can import from src/) ──────────────────────────────────
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

# ── Constants ─────────────────────────────────────────────────────────────────

_API_BASE   = "http://localhost:8080"
_BOT_HANDLE = "rudagency_bot"
_POLL_S     = 30          # seconds between auto-refresh

# Read token from .env (graceful fallback to empty)
def _read_token() -> str:
    env_file = _REPO / ".env"
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("DASHBOARD_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

_TOKEN = _read_token()
_HEADERS = {"X-Dashboard-Token": _TOKEN} if _TOKEN else {}

# Menu bar icons — plain ASCII/Unicode (emoji breaks on some macOS versions)
_ICON_OK      = "▲"    # connected
_ICON_OFFLINE = "△"    # disconnected

# ── API helpers ───────────────────────────────────────────────────────────────

def _get(path: str, timeout: float = 5.0) -> Optional[dict[str, Any]]:
    try:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as c:
            r = c.get(f"{_API_BASE}{path}")
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def _post(path: str, body: dict[str, Any], timeout: float = 90.0) -> Optional[dict[str, Any]]:
    try:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as c:
            r = c.post(f"{_API_BASE}{path}", json=body)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


# ── App ───────────────────────────────────────────────────────────────────────

class AURABar(rumps.App):
    def __init__(self) -> None:
        super().__init__(name="AURA", title=_ICON_OFFLINE, quit_button=None)

        self._voice_on: bool      = False
        self._brains_data: Optional[dict] = None

        # Menu items (built once, titles updated dynamically)
        self._m_status    = rumps.MenuItem("△  AURA · conectando…")
        self._m_sep0      = rumps.separator
        self._m_chat      = rumps.MenuItem("✏️  Chat…",            callback=self._chat)
        self._m_voice     = rumps.MenuItem("🔊  Voz: OFF",          callback=self._toggle_voice)
        self._m_sep1      = rumps.separator
        self._m_limits    = rumps.MenuItem("📊  Rate limits",       callback=self._show_limits)
        self._m_refresh   = rumps.MenuItem("🔄  Actualizar",        callback=self._refresh)
        self._m_sep2      = rumps.separator
        self._m_telegram  = rumps.MenuItem("✈️  Abrir Telegram",    callback=self._open_telegram)
        self._m_quit      = rumps.MenuItem("✕  Salir",             callback=lambda _: rumps.quit_application())

        self.menu = [
            self._m_status,
            self._m_sep0,
            self._m_chat,
            self._m_voice,
            self._m_sep1,
            self._m_limits,
            self._m_refresh,
            self._m_sep2,
            self._m_telegram,
            self._m_sep2,
            self._m_quit,
        ]

        # Timer-based polling (avoids threading + UI calls from wrong thread)
        self._poll_timer = rumps.Timer(self._on_poll, _POLL_S)
        self._poll_timer.start()
        # First update after 1s (give the app time to draw)
        rumps.Timer(self._on_first_poll, 1).start()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _on_first_poll(self, sender: rumps.Timer) -> None:
        sender.stop()
        self._do_refresh()

    def _on_poll(self, _: rumps.Timer) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        """Fetch /api/brains and update menu (always called on main thread via Timer)."""
        data = _get("/api/brains")
        self._brains_data = data
        if data:
            self.title = _ICON_OK
            best = data.get("best_available", "?")
            avail = sum(
                1 for b in data.get("brains", [])
                if b.get("available") and b["name"] in ("haiku","sonnet","opus","codex","gemini")
            )
            self._m_status.title = f"▲  AURA · {best}  ({avail}/5 ok)"
        else:
            self.title = _ICON_OFFLINE
            self._m_status.title = "△  AURA · offline"

    # ── Chat ─────────────────────────────────────────────────────────────────

    def _chat(self, _: rumps.MenuItem) -> None:
        """Open text input, call /api/chat synchronously, show response.
        All on main thread — avoids threading crashes."""
        win = rumps.Window(
            message="Escribe tu mensaje para AURA:",
            title="Chat con AURA",
            default_text="",
            ok="Enviar",
            cancel="Cancelar",
            dimensions=(420, 80),
        )
        resp = win.run()
        if not resp.clicked:
            return
        msg = resp.text.strip()
        if not msg:
            return

        # Show "thinking" state in menu bar while API call runs
        _prev_title = self.title
        self.title = "…"

        t0 = time.time()
        result = _post("/api/chat", {"message": msg, "user_id": 0}, timeout=120.0)
        elapsed = int((time.time() - t0) * 1000)

        self.title = _prev_title

        if result is None:
            rumps.alert(
                title="AURA offline",
                message="No se pudo contactar con el servidor (¿está corriendo el bot?)",
                ok="OK",
            )
            return

        content: str = result.get("content") or "(sin respuesta)"
        brain: str   = result.get("brain_display") or result.get("brain") or "?"
        ok: bool     = result.get("ok", True)

        if not ok:
            rumps.alert(title="Error de AURA", message=content[:500], ok="OK")
            return

        # Show full response
        # rumps.alert wraps long text — show first 800 chars
        display = content if len(content) <= 800 else content[:797] + "…"
        rumps.alert(
            title=f"AURA · {brain}  ({elapsed}ms)",
            message=display,
            ok="OK",
        )

    # ── Voice toggle ─────────────────────────────────────────────────────────

    def _toggle_voice(self, _: rumps.MenuItem) -> None:
        self._voice_on = not self._voice_on
        cmd   = "/voz on"  if self._voice_on else "/voz off"
        label = "🔊  Voz: ON" if self._voice_on else "🔊  Voz: OFF"
        self._m_voice.title = label
        _post("/api/invoke", {"command": cmd}, timeout=10.0)

    # ── Rate limits ──────────────────────────────────────────────────────────

    def _show_limits(self, _: rumps.MenuItem) -> None:
        """Show unified rate-limit card (same format as /status in Telegram)."""
        data = _get("/api/brains") or self._brains_data

        try:
            from src.bot.utils.rate_card import build_rate_card
            card = build_rate_card(data, html=False)
        except Exception:
            card = _fallback_card(data)

        rumps.alert(title="📊 Rate Limits", message=card, ok="OK")

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _refresh(self, _: rumps.MenuItem) -> None:
        self._do_refresh()

    def _open_telegram(self, _: rumps.MenuItem) -> None:
        os.system(f"open 'tg://resolve?domain={_BOT_HANDLE}'")


# ── Fallback card (if src/ isn't importable from menu bar context) ───────────

def _fallback_card(data: Optional[dict]) -> str:
    if not data:
        return "AURA offline"
    lines = [f"🧠 Brains — {data.get('best_available','?')} activo", ""]
    for b in data.get("brains", []):
        name = b.get("name","?")
        req  = b.get("requests", 0)
        lim  = b.get("limit","∞")
        rl   = b.get("is_rate_limited", False)
        status = "⛔" if rl else "✅"
        lines.append(f"{status} {name}: {req}/{lim}")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AURABar().run()
