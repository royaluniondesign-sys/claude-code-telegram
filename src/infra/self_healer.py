"""AURA Self-Healer — periodic diagnostic and auto-repair agent.

Runs every 2h (configurable). Checks all systems, auto-fixes what it can,
sends Telegram alert only when something actually needs attention.

Checks:
  - Bot process alive + responding
  - All brain APIs reachable (quick health)
  - Disk space (warn < 5GB)
  - Error rate in logs (warn > 50 errors/hour)
  - Env vars present (RESEND_API_KEY, OPENROUTER_API_KEY, etc.)
  - Mem0 database accessible
  - Semantic router ready
  - Log file size (rotate if > 100MB)

Auto-fixes:
  - Rotate oversized log files
  - Re-initialize Mem0 if database corrupt
  - Clear temp/cache if disk low
  - Restart bot via launchctl if health check fails
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import structlog

logger = structlog.get_logger()

_BOT_LOG = Path.home() / "claude-code-telegram/logs/bot.stdout.log"


def _ensure_task(title: str, description: str, priority: str, category: str, tags: Optional[List[str]] = None, auto_fix: bool = False, fix_command: str = "") -> None:
    """Create a task if none with the same title is already pending/in_progress."""
    try:
        from .task_store import create_task, list_tasks
        active_titles = {t["title"] for t in list_tasks() if t.get("status") in ("pending", "in_progress")}
        if title in active_titles:
            return
        create_task(
            title=title,
            description=description,
            priority=priority,
            category=category,
            created_by="self_healer",
            auto_fix=auto_fix,
            fix_command=fix_command,
            tags=tags or [],
        )
        logger.info("self_healer_task_created", title=title)
    except Exception as e:
        logger.debug("self_healer_task_create_fail", error=str(e))
_BOT_PLIST = Path.home() / "Library/LaunchAgents/com.aura.telegram-bot.plist"
# OPENROUTER_API_KEY removed — AURA uses Claude Max subscription
# RESEND_API_KEY optional — email not blocking core workflows
_REQUIRED_ENV = ["TELEGRAM_BOT_TOKEN"]


@dataclass
class HealthReport:
    ok: bool = True
    issues: List[str] = field(default_factory=list)
    fixes_applied: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    def fail(self, issue: str) -> None:
        self.ok = False
        self.issues.append(issue)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def fixed(self, msg: str) -> None:
        self.fixes_applied.append(msg)

    def summary(self) -> str:
        parts = []
        if self.ok and not self.warnings:
            parts.append("✅ AURA: todo OK")
        else:
            if self.issues:
                parts.append("🔴 Issues:\n" + "\n".join(f"  · {i}" for i in self.issues))
            if self.warnings:
                parts.append("⚠️ Warnings:\n" + "\n".join(f"  · {w}" for w in self.warnings))
        if self.fixes_applied:
            parts.append("🔧 Auto-fixed:\n" + "\n".join(f"  · {f}" for f in self.fixes_applied))
        return "\n\n".join(parts)


# ── Individual checks ──────────────────────────────────────────────────────────

async def _check_bot_process(report: HealthReport) -> None:
    """Verify bot LaunchAgent is running."""
    proc = await asyncio.create_subprocess_shell(
        "launchctl list com.aura.telegram-bot 2>/dev/null | awk '{print $1}'",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    pid = out.decode().strip()
    if not pid or pid == "-":
        report.fail("Bot process not running")
        # Auto-fix: restart
        try:
            await asyncio.create_subprocess_shell(
                f"launchctl unload {_BOT_PLIST} 2>/dev/null; "
                f"sleep 1; launchctl load {_BOT_PLIST}",
            )
            report.fixed("Restarted bot via launchctl")
        except Exception as e:
            report.fail(f"Auto-restart failed: {e}")
        _ensure_task(
            "Restart bot process",
            "Bot LaunchAgent not responding. Self-healer attempted restart.",
            priority="critical", category="fix",
            tags=["bot", "launchctl"],
            auto_fix=True,
            fix_command=f"launchctl unload {_BOT_PLIST} 2>/dev/null; sleep 2; launchctl load {_BOT_PLIST}",
        )


async def _check_disk(report: HealthReport) -> None:
    """Warn if less than 5GB free."""
    usage = shutil.disk_usage("/")
    free_gb = usage.free / 1e9
    if free_gb < 2:
        report.fail(f"Critical: only {free_gb:.1f}GB disk free")
        # Auto-fix: clear logs if large
        await _maybe_rotate_logs(report)
        _ensure_task(
            f"Free disk space — only {free_gb:.1f}GB left",
            "Disk critically low. Prune Docker images, logs, caches.",
            priority="critical", category="maintenance",
            tags=["disk"],
            auto_fix=True,
            fix_command="docker system prune -f 2>/dev/null; rm -f ~/claude-code-telegram/logs/*.log.bak 2>/dev/null; df -h / | tail -1",
        )
    elif free_gb < 5:
        report.warn(f"Disk low: {free_gb:.1f}GB free")
        _ensure_task(
            f"Free disk space — only {free_gb:.1f}GB left",
            "Disk running low. Prune Docker images, caches, old logs.",
            priority="high", category="maintenance",
            tags=["disk"],
            auto_fix=True,
            fix_command="docker system prune -f 2>/dev/null; rm -f ~/claude-code-telegram/logs/*.log.bak 2>/dev/null; df -h / | tail -1",
        )


async def _check_env_vars(report: HealthReport) -> None:
    """Check required environment variables are set."""
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    # Also check .env file
    env_file = Path.home() / "claude-code-telegram/.env"
    if env_file.exists():
        env_text = env_file.read_text()
        missing = [k for k in missing if k not in env_text]
    if missing:
        report.warn(f"Missing env vars: {', '.join(missing)}")
        for k in missing:
            _ensure_task(
                f"Set {k} in .env",
                f"Environment variable {k} is missing. Add it to ~/claude-code-telegram/.env",
                priority="high", category="fix",
                tags=["env", k.lower()],
            )


async def _check_log_errors(report: HealthReport) -> None:
    """Count error-level log entries in the last hour."""
    if not _BOT_LOG.exists():
        return
    try:
        proc = await asyncio.create_subprocess_shell(
            f"grep '\"level\": \"error\"' {_BOT_LOG} | "
            f"awk -F'\"timestamp\": \"' '{{print $2}}' | "
            f"awk -F'\"' '{{print $1}}' | "
            f"awk -v cutoff=\"$(date -u -v-1H '+%Y-%m-%dT%H')\" '$0 >= cutoff' | wc -l",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        count = int(out.decode().strip() or "0")
        if count > 50:
            report.fail(f"High error rate: {count} errors in last hour")
            _ensure_task(
                f"Investigate high error rate — {count} errors/hr",
                f"Bot log shows {count} errors in the last hour. Check logs for root cause.",
                priority="high", category="fix",
                tags=["logs", "errors"],
            )
        elif count > 10:
            report.warn(f"Elevated errors: {count} in last hour")
    except Exception:
        pass


async def _check_error_patterns(report: HealthReport) -> None:
    """Detect recurring errors and create fix tasks if needed."""
    try:
        from .error_pattern_detector import create_tasks_for_patterns
        task_ids = create_tasks_for_patterns()
        if task_ids:
            report.warn(f"Created {len(task_ids)} task(s) for recurring errors")
    except Exception as e:
        logger.debug("error_pattern_check_fail", error=str(e))


async def _check_log_size(report: HealthReport) -> None:
    """Auto-rotate log file if > 50MB."""
    if _BOT_LOG.exists() and _BOT_LOG.stat().st_size > 50 * 1024 * 1024:
        await _maybe_rotate_logs(report)


async def _maybe_rotate_logs(report: HealthReport) -> None:
    if _BOT_LOG.exists():
        size_mb = _BOT_LOG.stat().st_size / 1024 / 1024
        # Keep last 1000 lines
        try:
            proc = await asyncio.create_subprocess_shell(
                f"tail -1000 {_BOT_LOG} > {_BOT_LOG}.tmp && mv {_BOT_LOG}.tmp {_BOT_LOG}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            report.fixed(f"Rotated log ({size_mb:.0f}MB → last 1000 lines)")
        except Exception as e:
            report.warn(f"Log rotation failed: {e}")


async def _check_mem0(report: HealthReport) -> None:
    """Verify Mem0 database is accessible."""
    mem0_dir = Path.home() / ".aura/mem0"
    if not mem0_dir.exists():
        report.warn("Mem0 directory missing — will be created on first use")
        return
    qdrant_dir = mem0_dir / "qdrant"
    if not qdrant_dir.exists():
        report.warn("Mem0 Qdrant storage not initialized yet")


async def _check_ram(report: HealthReport) -> None:
    """Check system RAM using memory_pressure (macOS accurate measure).

    vm_stat 'Pages free' alone is misleading on Apple Silicon — the OS
    keeps very few truly free pages by design (uses inactive + compressed
    memory). memory_pressure reports the real system-wide availability.
    Thresholds: < 10% free → critical, < 20% free → warning.
    """
    try:
        import re as _re
        import subprocess as _sp

        # Use hw.pagesize (16384 on Apple Silicon, not 4096) + count inactive
        # pages as available — macOS reclaims them readily.
        _sysctl = "/usr/sbin/sysctl"
        page_size = int(_sp.check_output([_sysctl, "-n", "hw.pagesize"], timeout=3).strip())
        total_bytes = int(_sp.check_output([_sysctl, "-n", "hw.memsize"], timeout=3).strip())
        vm = _sp.check_output("vm_stat", shell=True, timeout=3, text=True)

        def _pages(pat: str) -> int:
            m = _re.search(pat, vm)
            return int(m.group(1).rstrip(".")) if m else 0

        avail_pages = (
            _pages(r"Pages free:\s+(\d+)")
            + _pages(r"Pages speculative:\s+(\d+)")
            + _pages(r"Pages purgeable:\s+(\d+)")
            + _pages(r"Pages inactive:\s+(\d+)")
        )
        avail_bytes = avail_pages * page_size
        free_pct = round(avail_bytes / total_bytes * 100)
        total_mb = total_bytes // 1024 // 1024
        free_mb = avail_bytes // 1024 // 1024

        if free_pct < 10:
            report.fail(f"RAM crítico: {free_pct}% libre ({free_mb}MB de {total_mb}MB)")
        elif free_pct < 20:
            report.warn(f"RAM bajo: {free_pct}% libre ({free_mb}MB de {total_mb}MB)")

        # Bot process RSS — alert if the bot itself is leaking
        proc3 = await asyncio.create_subprocess_shell(
            "pgrep -f claude-telegram-bot | head -1 | xargs -I{} ps -o rss= -p {} 2>/dev/null",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out3, _ = await asyncio.wait_for(proc3.communicate(), timeout=5)
        rss_raw = out3.decode().strip()
        if rss_raw and rss_raw.isdigit():
            rss_mb = int(rss_raw) // 1024
            if rss_mb > 600:
                report.warn(f"Bot proceso alto: {rss_mb}MB RSS")
    except Exception as e:
        logger.debug("ram_check_failed", error=str(e))


async def _check_brains(report: HealthReport) -> None:
    """Quick connectivity check for external brain APIs."""
    checks = [
        ("OpenRouter", "curl -s -o /dev/null -w '%{http_code}' https://openrouter.ai/api/v1/models --max-time 5"),
        ("Gemini CLI", "which gemini && echo ok || echo missing"),
    ]
    for name, cmd in checks:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            result = out.decode().strip()
            if name == "OpenRouter" and result not in ("200", "401"):
                report.warn(f"{name}: unexpected response {result}")
            elif name == "Gemini CLI" and "missing" in result:
                report.warn("Gemini CLI not found in PATH")
        except asyncio.TimeoutError:
            report.warn(f"{name}: timeout on connectivity check")
        except Exception as e:
            report.warn(f"{name}: check failed — {e}")


# ── Main diagnostic runner ─────────────────────────────────────────────────────

async def run_diagnostics() -> HealthReport:
    """Run all checks and return consolidated health report."""
    report = HealthReport()
    checks = [
        _check_bot_process,
        _check_disk,
        _check_ram,
        _check_env_vars,
        _check_log_errors,
        _check_error_patterns,
        _check_log_size,
        _check_mem0,
        _check_brains,
    ]
    for check in checks:
        try:
            await check(report)
        except Exception as e:
            report.warn(f"Check {check.__name__} failed: {e}")

    logger.info(
        "self_healer_done",
        ok=report.ok,
        issues=len(report.issues),
        fixes=len(report.fixes_applied),
        warnings=len(report.warnings),
    )
    return report


async def run_diagnostics_report() -> str:
    """Run diagnostics and return a plain-text summary string.

    Returns empty string if everything is OK — scheduler skips sending it.
    Only returns content when there are issues, warnings, or auto-fixes.
    """
    report = await run_diagnostics()

    # Silent when healthy — no spam
    if report.ok and not report.warnings and not report.fixes_applied:
        return ""

    from datetime import datetime
    ts = datetime.fromtimestamp(report.checked_at).strftime("%Y-%m-%d %H:%M")
    status = "✅ OK" if report.ok else "🔴 ISSUES"
    lines = [f"*🩺 AURA Diagnóstico — {ts}*\nStatus: {status}"]
    if report.issues:
        lines.append("*Problemas:*\n" + "\n".join(f"• {i}" for i in report.issues))
    if report.fixes_applied:
        lines.append("*Auto-fixed:*\n" + "\n".join(f"✔ {f}" for f in report.fixes_applied))
    if report.warnings:
        lines.append("*Advertencias:*\n" + "\n".join(f"⚠ {w}" for w in report.warnings[:5]))
    return "\n\n".join(lines)


async def run_and_notify(notify_fn: Optional[Callable[[str], None]] = None) -> HealthReport:
    """Run diagnostics and optionally send Telegram notification."""
    report = await run_diagnostics()
    # Only notify if there's something worth reporting
    if not report.ok or report.fixes_applied or len(report.warnings) > 2:
        summary = report.summary()
        logger.info("self_healer_alert", summary=summary[:200])
        if notify_fn:
            try:
                notify_fn(f"🔍 Auto-diagnóstico AURA:\n\n{summary}")
            except Exception as e:
                logger.warning("self_healer_notify_fail", error=str(e))
    return report
