#!/usr/bin/env python3
"""AURA Menu Bar App — macOS quick access without Telegram.

Requires: pip install rumps httpx
Run: python3 menubar/aura_bar.py
"""

import os
import threading
from typing import Any, Dict, Optional

import httpx
import rumps

# ── Constants ────────────────────────────────────────────────────────────────

API_BASE = "http://localhost:8080"
TELEGRAM_BOT_USERNAME = "rudagency_bot"
POLL_INTERVAL_SECONDS = 30

ICON_CONNECTED = "🤖"
ICON_DISCONNECTED = "⚠️"

# ── API helpers ───────────────────────────────────────────────────────────────


def _get(path: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Synchronous GET to the AURA API. Returns None on any error."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{API_BASE}{path}")
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


def _post(path: str, body: Dict[str, Any], timeout: float = 60.0) -> Optional[Dict[str, Any]]:
    """Synchronous POST to the AURA API. Returns None on any error."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{API_BASE}{path}", json=body)
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


def _fetch_status() -> Optional[Dict[str, Any]]:
    return _get("/api/status")


def _fetch_brains() -> Optional[Dict[str, Any]]:
    return _get("/api/brains")


def _send_message(message: str) -> Optional[Dict[str, Any]]:
    return _post("/api/chat", {"message": message, "user_id": 0})


def _invoke_command(command: str) -> Optional[Dict[str, Any]]:
    return _post("/api/invoke", {"command": command})


def _best_brain_name(brains_data: Optional[Dict[str, Any]]) -> str:
    """Extract the best-available brain display name, or 'offline'."""
    if not brains_data:
        return "offline"
    best = brains_data.get("best_available")
    if not best:
        return "offline"
    return best


# ── Menu Bar App ─────────────────────────────────────────────────────────────


class AURAMenuBarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(
            name="AURA",
            title=ICON_DISCONNECTED,
            quit_button=None,
        )

        self._voice_enabled: bool = False
        self._last_response: str = ""
        self._connected: bool = False

        # ── Menu items ────────────────────────────────────────────────────
        self._status_item = rumps.MenuItem("AURA · offline", callback=None)
        self._status_item.set_callback(None)  # non-clickable

        self._chat_item = rumps.MenuItem("Chat...", callback=self._on_chat)
        self._last_response_item = rumps.MenuItem("", callback=None)
        self._last_response_item.set_callback(None)  # non-clickable

        self._voice_item = rumps.MenuItem("🔊 Voz: OFF", callback=self._on_toggle_voice)
        self._brain_status_item = rumps.MenuItem("📊 Brain status", callback=self._on_brain_status)
        self._refresh_item = rumps.MenuItem("🔄 Actualizar", callback=self._on_refresh)

        self._telegram_item = rumps.MenuItem("Abrir Telegram", callback=self._on_open_telegram)
        self._quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self._status_item,
            self._chat_item,
            rumps.separator,
            self._last_response_item,
            rumps.separator,
            self._voice_item,
            self._brain_status_item,
            self._refresh_item,
            rumps.separator,
            self._telegram_item,
            rumps.separator,
            self._quit_item,
        ]

        # Start background poll timer
        self._timer = rumps.Timer(self._poll_status, POLL_INTERVAL_SECONDS)
        self._timer.start()

        # Initial status fetch (non-blocking)
        threading.Thread(target=self._refresh_status_async, daemon=True).start()

    # ── Status polling ────────────────────────────────────────────────────────

    def _poll_status(self, _: rumps.Timer) -> None:
        """Called by rumps.Timer every POLL_INTERVAL_SECONDS."""
        self._refresh_status_async()

    def _refresh_status_async(self) -> None:
        """Fetch status in a background thread and update UI."""
        brains_data = _fetch_brains()
        connected = brains_data is not None
        brain_name = _best_brain_name(brains_data)

        self._connected = connected
        self.title = ICON_CONNECTED if connected else ICON_DISCONNECTED

        if connected:
            self._status_item.title = f"AURA · {brain_name}"
        else:
            self._status_item.title = "AURA · offline"

    # ── Menu callbacks ────────────────────────────────────────────────────────

    def _on_chat(self, _: rumps.MenuItem) -> None:
        """Open input dialog, send message, show response."""
        window = rumps.Window(
            message="Escribe tu mensaje:",
            title="Chat con AURA",
            default_text="",
            ok="Enviar",
            cancel="Cancelar",
            dimensions=(400, 80),
        )
        response = window.run()
        if not response.clicked:
            return

        message = response.text.strip()
        if not message:
            return

        # Run the API call in a background thread to avoid blocking the UI
        threading.Thread(
            target=self._send_and_show,
            args=(message,),
            daemon=True,
        ).start()

    def _send_and_show(self, message: str) -> None:
        """POST to /api/chat and display result (runs in background thread)."""
        result = _send_message(message)

        if result is None:
            rumps.alert(
                title="Error",
                message="No se pudo contactar con AURA. ¿Está corriendo el servidor?",
                ok="OK",
            )
            return

        if not result.get("ok", True):
            error = result.get("error", "Error desconocido")
            rumps.alert(title="Error de AURA", message=error, ok="OK")
            return

        content: str = result.get("content", "(sin respuesta)")
        brain: str = result.get("brain_display") or result.get("brain", "?")
        duration_ms: int = result.get("duration_ms", 0)

        # Store last response (truncated for menu display)
        self._last_response = content
        snippet = content[:80] + "…" if len(content) > 80 else content
        self._last_response_item.title = snippet

        rumps.alert(
            title=f"AURA ({brain}) · {duration_ms}ms",
            message=content,
            ok="OK",
        )

    def _on_toggle_voice(self, _: rumps.MenuItem) -> None:
        """Toggle voice mode via /api/invoke."""
        self._voice_enabled = not self._voice_enabled
        command = "/voz on" if self._voice_enabled else "/voz off"
        label = "🔊 Voz: ON" if self._voice_enabled else "🔊 Voz: OFF"
        self._voice_item.title = label

        threading.Thread(
            target=lambda: _invoke_command(command),
            daemon=True,
        ).start()

    def _on_brain_status(self, _: rumps.MenuItem) -> None:
        """Fetch brain health and show in a dialog."""
        data = _fetch_brains()
        if data is None:
            rumps.alert(
                title="Brain Status",
                message="AURA offline — no se pudo obtener el estado.",
                ok="OK",
            )
            return

        brains = data.get("brains", [])
        best = data.get("best_available", "ninguno")
        any_available = data.get("any_available", False)

        lines = [f"Mejor disponible: {best}", ""]
        for b in brains:
            name = b.get("name", "?")
            status = b.get("status", "?")
            pct = b.get("usage_pct")
            rl = b.get("is_rate_limited", False)

            if rl:
                recover = b.get("recover_in_str", "?")
                lines.append(f"⛔ {name}: rate limited (recupera en {recover})")
            elif status == "warn":
                lines.append(f"⚠️  {name}: {pct}% usado")
            else:
                lines.append(f"✅ {name}: OK{f' ({pct}%)' if pct is not None else ''}")

        if not any_available:
            lines.append("\n⚠️ Ningún brain disponible.")

        rumps.alert(
            title="📊 Estado de los Brains",
            message="\n".join(lines),
            ok="OK",
        )

    def _on_refresh(self, _: rumps.MenuItem) -> None:
        """Manually trigger a status refresh."""
        threading.Thread(target=self._refresh_status_async, daemon=True).start()

    def _on_open_telegram(self, _: rumps.MenuItem) -> None:
        """Open the AURA bot in Telegram."""
        os.system(f"open 'tg://resolve?domain={TELEGRAM_BOT_USERNAME}'")

    def _on_quit(self, _: rumps.MenuItem) -> None:
        rumps.quit_application()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AURAMenuBarApp().run()
