"""Unified rate limit card formatter — same output for Telegram, menu bar, dashboard.

All consumers call build_rate_card(brains_data) to get a consistent display.
"""
from __future__ import annotations

from typing import Any

# Unicode progress bar blocks
_FULL  = "█"
_EMPTY = "░"
_BAR_W = 10


def _bar(pct: float) -> str:
    """0-100 → 10-char progress bar."""
    filled = round(pct / 100 * _BAR_W)
    filled = max(0, min(_BAR_W, filled))
    return _FULL * filled + _EMPTY * (_BAR_W - filled)


def _status_emoji(b: dict[str, Any]) -> str:
    if b.get("is_rate_limited"):
        return "⛔"
    pct = b.get("usage_pct") or 0
    if pct >= 90:
        return "🔴"
    if pct >= 70:
        return "🟡"
    return "✅"


# Brains to show (ordered, with short display names)
_SHOW = [
    ("haiku",   "Haiku  "),
    ("sonnet",  "Sonnet "),
    ("opus",    "Opus   "),
    ("codex",   "Codex  "),
    ("gemini",  "Gemini "),
]


def build_rate_card(brains_data: dict[str, Any] | None, *, html: bool = False) -> str:
    """Build a compact rate-limit card from /api/brains response.

    Args:
        brains_data: JSON from GET /api/brains (with auth).
        html:        If True, wrap in <code> for Telegram HTML parse_mode.

    Returns:
        Formatted string ready to display.
    """
    if not brains_data:
        if html:
            return "⚠️ AURA offline — no se pudo obtener datos."
        return "⚠️  AURA offline"

    brain_map: dict[str, dict[str, Any]] = {
        b["name"]: b for b in brains_data.get("brains", [])
    }

    lines: list[str] = []

    # Window reset header (use the shortest remaining window)
    window_strs: list[str] = []
    for key, _ in _SHOW:
        b = brain_map.get(key)
        if b and b.get("window_remaining_str"):
            window_strs.append((b["window_remaining_seconds"], b["window_remaining_str"]))  # type: ignore[arg-type]
    if window_strs:
        window_strs.sort()
        lines.append(f"🧠 Brains · próximo reset: {window_strs[0][1]}")
    else:
        lines.append("🧠 Brains")

    lines.append("─" * 32)

    for key, label in _SHOW:
        b = brain_map.get(key)
        if not b:
            continue

        emoji = _status_emoji(b)
        pct   = b.get("usage_pct") or 0.0
        req   = b.get("requests", 0)
        lim   = b.get("limit") or "∞"
        win   = b.get("window", "")
        win_short = win.replace(" rolling", "").replace("daily", "24h").replace("none", "∞")

        bar = _bar(pct)

        if b.get("is_rate_limited"):
            rec = b.get("recover_in_str") or "?"
            row = f"{emoji} {label} {bar}  ⛔ recupera en {rec}"
        else:
            row = f"{emoji} {label} {bar}  {req}/{lim} ({win_short})"

        lines.append(row)

    lines.append("─" * 32)

    best = brains_data.get("best_available", "ninguno")
    any_ok = brains_data.get("any_available", False)
    if any_ok:
        lines.append(f"▶ Activo: {best}")
    else:
        lines.append("⚠️  Ningún brain disponible — todo rate-limitado")

    text = "\n".join(lines)
    if html:
        return f"<code>{text}</code>"
    return text


def build_rate_card_short(brains_data: dict[str, Any] | None) -> str:
    """One-line summary for menu bar status item."""
    if not brains_data:
        return "AURA · offline"
    best = brains_data.get("best_available", "?")
    # Count available brains
    available = sum(
        1 for b in brains_data.get("brains", [])
        if b.get("available") and b["name"] in {k for k, _ in _SHOW}
    )
    return f"AURA · {best} ({available}/5 ok)"
