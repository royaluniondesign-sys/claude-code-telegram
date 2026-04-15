"""AURA Meta-Context — Universal self-knowledge for the cognitive loop.

This is the ADENTRO layer: AURA's self-awareness that transcends any specific task.
Whether the task is "fix Telegram crash" or "write SEO for Madrid client", the same
meta-context feeds into every conductor run — internal (self-improvement) and
external (Ricardo's requests).

What it provides:
  - Conductor history: what was tried, what worked, what failed and why
  - Brain performance: which brains are healthy, which are rate-limited
  - Error patterns: recurring failures that need persistent attention
  - Mission progress: what high-priority items remain unchecked
  - Anti-repetition: tasks that failed repeatedly — never try the same approach again

Usage:
    from src.infra.meta_context import build_compact_context, build_full_context

    # Compact (~400 chars) — for planner prompt injection (haiku)
    ctx = build_compact_context()

    # Full (~2000 chars) — for L1 diagnosis step (local-ollama)
    ctx = build_full_context()
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

_AURA_ROOT = Path.home() / "claude-code-telegram"
_LOG_PATH = _AURA_ROOT / "logs" / "bot.stderr.log"
_CONDUCTOR_LOG = Path.home() / ".aura" / "memory" / "conductor_log.md"
_MISSION_PATH = _AURA_ROOT / "MISSION.md"
_USAGE_FILE = Path.home() / ".aura" / "usage.json"


# ── Brain health snapshot ─────────────────────────────────────────────────────

def _brain_health_summary() -> str:
    """Compact brain health from rate monitor. Never crashes."""
    try:
        from .rate_monitor import get_global_monitor
        monitor = get_global_monitor()
        parts = []
        for u in monitor.get_all_usage():
            if u.last_request == 0:
                continue  # never used — skip
            status = "⛔RL" if u.is_rate_limited else ("⚠️" if (u.usage_pct or 0) >= 0.75 else "✅")
            parts.append(f"{u.brain_name}:{u.requests_in_window}req/{u.errors_in_window}err{status}")
        return ", ".join(parts) if parts else "no usage yet"
    except Exception:
        return "unavailable"


# ── Conductor history parser ──────────────────────────────────────────────────

def _parse_conductor_log(max_entries: int = 8) -> list[dict]:
    """Parse recent entries from conductor_log.md into structured dicts."""
    entries: list[dict] = []
    if not _CONDUCTOR_LOG.exists():
        return entries
    try:
        text = _CONDUCTOR_LOG.read_text(errors="replace")
        # Each entry starts with "## YYYY-MM-DD HH:MM — run_id"
        blocks = re.split(r"\n(?=## \d{4}-\d{2}-\d{2})", text.strip())
        for block in reversed(blocks[-max_entries:]):  # newest first
            ts_match = re.search(r"## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) — (\S+)", block)
            task_match = re.search(r"\*\*Task:\*\* (.+)", block)
            result_match = re.search(r"\*\*Result:\*\* (.+)", block)
            if ts_match and task_match:
                entries.append({
                    "ts": ts_match.group(1),
                    "run_id": ts_match.group(2),
                    "task": task_match.group(1).strip()[:80],
                    "result": result_match.group(1).strip()[:100] if result_match else "?",
                    "ok": "COMMITTED" in (result_match.group(1) if result_match else ""),
                    "failed": "FAILED" in (result_match.group(1) if result_match else ""),
                })
    except Exception:
        pass
    return entries


# ── Error pattern reader ──────────────────────────────────────────────────────

def _recent_error_patterns(n_lines: int = 200) -> list[str]:
    """Extract unique recent error types from stderr log."""
    errors: list[str] = []
    seen: set[str] = set()
    try:
        if not _LOG_PATH.exists():
            return errors
        # Read last N lines efficiently
        result = subprocess.run(
            ["tail", "-n", str(n_lines), str(_LOG_PATH)],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            # Extract key part of error lines
            if "Error" in line or "error" in line or "Traceback" in line or "Exception" in line:
                # Normalize: strip timestamps and variable parts
                clean = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.Z]+", "", line)
                clean = re.sub(r'"[^"]{40,}"', '"..."', clean)  # long strings
                clean = clean.strip()[:120]
                if clean and clean not in seen:
                    seen.add(clean)
                    errors.append(clean)
                    if len(errors) >= 5:
                        break
    except Exception:
        pass
    return errors


# ── Mission progress reader ───────────────────────────────────────────────────

def _mission_progress() -> tuple[list[str], list[str]]:
    """Return (done_items, pending_items) from MISSION.md checkboxes."""
    done: list[str] = []
    pending: list[str] = []
    try:
        if not _MISSION_PATH.exists():
            return done, pending
        for line in _MISSION_PATH.read_text(errors="replace").splitlines():
            if "- [x]" in line or "- [X]" in line:
                done.append(line.strip().replace("- [x]", "").replace("- [X]", "").strip()[:60])
            elif "- [ ]" in line:
                pending.append(line.strip().replace("- [ ]", "").strip()[:60])
    except Exception:
        pass
    return done, pending


# ── Failed task history ───────────────────────────────────────────────────────

def _failed_task_titles() -> list[str]:
    """Task titles that failed 3+ times — should not be retried with same approach."""
    titles: list[str] = []
    try:
        from .task_store import list_tasks
        for t in list_tasks():
            if t.get("status") == "failed" and (t.get("attempts") or 0) >= 3:
                titles.append(t.get("title", "")[:60])
    except Exception:
        pass
    return titles[:5]


# ── Context builders ──────────────────────────────────────────────────────────

def build_compact_context() -> str:
    """Build ~400-char self-knowledge summary for planner prompt injection.

    Used in conductor._create_plan() — haiku planner gets AURA's self-knowledge
    before deciding layers/brains/strategy for ANY task (internal or external).
    """
    parts: list[str] = []

    # Brain health
    health = _brain_health_summary()
    parts.append(f"Brains: {health}")

    # Recent conductor outcomes (last 5)
    history = _parse_conductor_log(max_entries=5)
    if history:
        outcomes = []
        for h in history[:5]:
            icon = "✅" if h["ok"] else ("❌" if h["failed"] else "⚠️")
            outcomes.append(f"{icon}{h['task'][:40]}")
        parts.append("Recent runs: " + " | ".join(outcomes))

    # Failed tasks (don't repeat same approach)
    failed = _failed_task_titles()
    if failed:
        parts.append("Exhausted (3 fails, change approach): " + ", ".join(failed))

    # Mission priorities (top 3 pending Tier 1/2)
    _, pending = _mission_progress()
    if pending:
        parts.append("Mission priorities: " + " | ".join(pending[:3]))

    return "\n".join(parts)


def build_full_context() -> str:
    """Build ~2000-char rich self-knowledge for L1 diagnosis injection.

    Used in _build_task_plan() L1 step — local-ollama diagnoser has full
    AURA history before analyzing what to change.
    """
    sections: list[str] = []

    # --- Conductor history (full, with results) ---
    history = _parse_conductor_log(max_entries=8)
    if history:
        lines = ["### Recent Conductor Runs (newest first):"]
        for h in history:
            icon = "✅ COMMITTED" if h["ok"] else ("❌ FAILED" if h["failed"] else "⚠️ NO-COMMIT")
            lines.append(f"  {h['ts']} [{h['run_id']}] {icon}")
            lines.append(f"    Task: {h['task']}")
            lines.append(f"    Result: {h['result']}")
        sections.append("\n".join(lines))

    # --- Brain health ---
    health = _brain_health_summary()
    sections.append(f"### Brain Health:\n  {health}")

    # --- Recent errors ---
    errors = _recent_error_patterns()
    if errors:
        sections.append("### Recent Errors (from stderr log):\n" + "\n".join(f"  {e}" for e in errors))

    # --- Mission progress ---
    done, pending = _mission_progress()
    if done or pending:
        lines = ["### Mission Progress:"]
        if done:
            lines.append("  Completed: " + ", ".join(done[:5]))
        if pending:
            lines.append("  Pending (priority order): " + " | ".join(pending[:6]))
        sections.append("\n".join(lines))

    # --- Exhausted tasks (approach must change) ---
    failed = _failed_task_titles()
    if failed:
        sections.append(
            "### Tasks exhausted (3 attempts failed — must change approach, not retry same way):\n"
            + "\n".join(f"  ✗ {t}" for t in failed)
        )

    return "\n\n".join(sections)


def build_outcome_context(task_title: str, run_id: str) -> Optional[str]:
    """Build a 5-minute post-commit outcome check prompt.

    After a conductor run commits code, this prompt is used by local-ollama
    to verify the fix actually worked by checking recent log lines.
    Returns None if no meaningful check can be done.
    """
    try:
        if not _LOG_PATH.exists():
            return None
        result = subprocess.run(
            ["tail", "-n", "50", str(_LOG_PATH)],
            capture_output=True, text=True, timeout=3,
        )
        recent_log = result.stdout.strip()[-1500:]
        if not recent_log:
            return None
        return (
            f"After committing fix for: '{task_title}' (run {run_id})\n\n"
            f"Recent bot stderr (last 50 lines):\n{recent_log}\n\n"
            f"Does the error this fix targeted still appear? Answer: YES / NO / UNCLEAR\n"
            f"If YES: what was missed? If NO: confirm fix is effective."
        )
    except Exception:
        return None
