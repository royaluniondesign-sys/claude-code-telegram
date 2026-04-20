#!/usr/bin/env python3
"""AURA Menu Bar App — acceso rápido sin abrir Telegram.

Requires: uv add rumps httpx  (already in project deps)
Run:      python3 menubar/aura_bar.py
Auto-start: bash menubar/install.sh
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import rumps

# ── Path setup ────────────────────────────────────────────────────────────────
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

# ── Constants ─────────────────────────────────────────────────────────────────
_API_BASE   = "http://localhost:8080"
_BOT_HANDLE = "rudagency_bot"
_POLL_S     = 30

def _read_token() -> str:
    try:
        for line in (_REPO / ".env").read_text().splitlines():
            if line.startswith("DASHBOARD_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

_TOKEN   = _read_token()
_HEADERS = {"X-Dashboard-Token": _TOKEN} if _TOKEN else {}

_ICON_OK      = "▲"
_ICON_OFFLINE = "△"

# ── macOS native dialogs via osascript ────────────────────────────────────────
# Avoids rumps.Window / rumps.alert crashes — osascript is a system service.

def _ask(prompt: str, title: str = "AURA", default: str = "") -> Optional[str]:
    """Show a native macOS text input dialog. Returns text or None if cancelled."""
    script = (
        f'display dialog "{prompt}" '
        f'default answer "{default}" '
        f'with title "{title}" '
        f'buttons {{"Cancelar", "Enviar"}} '
        f'default button "Enviar"'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        return None  # user cancelled or error
    out = result.stdout.strip()
    if "text returned:" in out:
        return out.split("text returned:", 1)[1].strip()
    return None


def _say(message: str, title: str = "AURA") -> None:
    """Show a native macOS alert dialog (always works, no thread issues)."""
    # Truncate for display — osascript has a limit
    display = message[:1200] + "…" if len(message) > 1200 else message
    # Escape quotes
    display = display.replace('"', '\\"').replace("'", "\\'")
    title   = title.replace('"', '\\"')
    script = (
        f'display dialog "{display}" '
        f'with title "{title}" '
        f'buttons {{"OK"}} '
        f'default button "OK"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=300)


def _notify(title: str, subtitle: str, message: str = "") -> None:
    """macOS notification (non-blocking, shown in notification center)."""
    script = (
        f'display notification "{message}" '
        f'with title "{title}" subtitle "{subtitle}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


# ── API helpers ───────────────────────────────────────────────────────────────

def _get(path: str, timeout: float = 5.0) -> Optional[dict[str, Any]]:
    try:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as c:
            r = c.get(f"{_API_BASE}{path}")
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def _post(path: str, body: dict[str, Any], timeout: float = 120.0) -> Optional[dict[str, Any]]:
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

        self._voice_on    : bool            = False
        self._brains_data : Optional[dict]  = None

        self._m_status   = rumps.MenuItem("△  AURA · conectando…")
        self._m_chat     = rumps.MenuItem("✏️  Chat…",           callback=self._chat)
        self._m_voice    = rumps.MenuItem("🔊  Voz: OFF",         callback=self._toggle_voice)
        self._m_limits   = rumps.MenuItem("📊  Rate limits",      callback=self._show_limits)
        self._m_refresh  = rumps.MenuItem("🔄  Actualizar",       callback=self._refresh)
        self._m_telegram = rumps.MenuItem("✈️  Abrir Telegram",   callback=self._open_telegram)
        self._m_quit     = rumps.MenuItem("✕  Salir",            callback=lambda _: rumps.quit_application())

        self.menu = [
            self._m_status,
            rumps.separator,
            self._m_chat,
            self._m_voice,
            rumps.separator,
            self._m_limits,
            self._m_refresh,
            rumps.separator,
            self._m_telegram,
            rumps.separator,
            self._m_quit,
        ]

        self._poll_timer = rumps.Timer(self._on_poll, _POLL_S)
        self._poll_timer.start()
        rumps.Timer(self._on_first_poll, 1).start()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _on_first_poll(self, sender: rumps.Timer) -> None:
        sender.stop()
        self._do_refresh()

    def _on_poll(self, _: rumps.Timer) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        data = _get("/api/brains")
        self._brains_data = data
        if data:
            self.title = _ICON_OK
            best  = data.get("best_available", "?")
            avail = sum(
                1 for b in data.get("brains", [])
                if b.get("available") and b["name"] in ("haiku","sonnet","opus","codex","gemini")
            )
            self._m_status.title = f"▲  AURA · {best}  ({avail}/5 ok)"
        else:
            self.title = _ICON_OFFLINE
            self._m_status.title = "△  AURA · offline"

    # ── Chat (osascript — never crashes) ─────────────────────────────────────

    def _chat(self, _: rumps.MenuItem) -> None:
        msg = _ask("Escribe tu mensaje para AURA:", title="Chat con AURA")
        if not msg:
            return

        _prev = self.title
        self.title = "…"

        t0     = time.time()
        result = _post("/api/chat", {"message": msg, "user_id": 0}, timeout=120.0)
        ms     = int((time.time() - t0) * 1000)

        self.title = _prev

        if result is None:
            _say("No se pudo contactar con AURA.\n¿Está corriendo el servidor?", title="AURA offline")
            return

        content = result.get("content") or "(sin respuesta)"
        brain   = result.get("brain_display") or result.get("brain") or "?"
        ok      = result.get("ok", True)

        if not ok:
            _say(content[:600], title="Error de AURA")
            return

        _say(content, title=f"AURA · {brain}  ({ms}ms)")

    # ── Voice toggle ─────────────────────────────────────────────────────────

    def _toggle_voice(self, _: rumps.MenuItem) -> None:
        self._voice_on = not self._voice_on
        cmd   = "/voz on"  if self._voice_on else "/voz off"
        label = "🔊  Voz: ON" if self._voice_on else "🔊  Voz: OFF"
        self._m_voice.title = label
        _post("/api/invoke", {"command": cmd}, timeout=10.0)

    # ── Rate limits ──────────────────────────────────────────────────────────

    def _show_limits(self, _: rumps.MenuItem) -> None:
        data = _get("/api/brains") or self._brains_data
        try:
            from src.bot.utils.rate_card import build_rate_card
            card = build_rate_card(data, html=False)
        except Exception:
            card = _fallback_card(data)
        _say(card, title="📊 Rate Limits")

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _refresh(self, _: rumps.MenuItem) -> None:
        self._do_refresh()

    def _open_telegram(self, _: rumps.MenuItem) -> None:
        os.system(f"open 'tg://resolve?domain={_BOT_HANDLE}'")


# ── Fallback card ─────────────────────────────────────────────────────────────

def _fallback_card(data: Optional[dict]) -> str:
    if not data:
        return "AURA offline"
    lines = [f"Brains — {data.get('best_available','?')} activo", ""]
    for b in data.get("brains", []):
        name = b.get("name", "?")
        req  = b.get("requests", 0)
        lim  = b.get("limit", "∞")
        rl   = b.get("is_rate_limited", False)
        lines.append(("⛔" if rl else "✅") + f" {name}: {req}/{lim}")
    return "\n".join(lines)


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AURABar().run()
