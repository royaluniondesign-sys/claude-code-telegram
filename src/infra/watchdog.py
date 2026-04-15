"""Watchdog — monitors and self-heals AURA services.

Checks every 5 minutes:
- Telegram bot responsive
- CLI executors available (ollama, claude, codex, gemini)
- Disk space
- Memory usage

Self-repair: restart failed services up to 3 times, then notify owner (once).
"""

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Max auto-restart attempts before giving up and notifying
_MAX_RETRIES = 3

# Services to monitor — only what's actually running
_LAUNCHAGENTS = {
    "com.aura.telegram-bot": {
        "plist": Path.home() / "Library/LaunchAgents/com.aura.telegram-bot.plist",
        "check_cmd": "launchctl list com.aura.telegram-bot 2>/dev/null | grep -c PID",
        "name": "AURA Bot",
        "emoji": "🤖",
    },
}

_BRAIN_CLIS = {
    "ollama": {"cmd": "ollama", "name": "Ollama"},
    "claude": {"cmd": "claude", "name": "Claude CLI"},
    "codex": {"cmd": "codex", "name": "Codex CLI"},
    "gemini": {"cmd": "gemini", "name": "Gemini CLI"},
}


@dataclass
class ServiceStatus:
    """Status of a monitored service."""

    name: str
    emoji: str
    is_running: bool
    details: str = ""
    error: Optional[str] = None


@dataclass
class HealthReport:
    """Full health report."""

    timestamp: float
    services: List[ServiceStatus] = field(default_factory=list)
    disk_free_gb: float = 0.0
    memory_used_pct: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def all_healthy(self) -> bool:
        return all(s.is_running for s in self.services) and not self.warnings


class Watchdog:
    """Monitors services and performs self-healing."""

    def __init__(self, notify_callback: Optional[Any] = None) -> None:
        """Initialize watchdog.

        Args:
            notify_callback: async callable(message: str) to send alerts.
        """
        self._notify = notify_callback
        self._failure_counts: Dict[str, int] = {}
        self._notified: set[str] = set()  # Services already notified — don't spam

    async def check_all(self) -> HealthReport:
        """Run full health check."""
        report = HealthReport(timestamp=time.time())

        # Check LaunchAgent services
        for service_id, info in _LAUNCHAGENTS.items():
            status = await self._check_launchagent(service_id, info)
            report.services.append(status)

        # Check brain CLIs
        for brain_name, info in _BRAIN_CLIS.items():
            status = await self._check_cli(brain_name, info)
            report.services.append(status)

        # Disk space
        try:
            usage = shutil.disk_usage(str(Path.home()))
            report.disk_free_gb = round(usage.free / (1024 ** 3), 1)
            if report.disk_free_gb < 5:
                report.warnings.append(f"⚠️ Low disk: {report.disk_free_gb}GB free")
        except Exception:
            pass

        # Memory (macOS)
        try:
            proc = await asyncio.create_subprocess_shell(
                "vm_stat | head -5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            # Parse vm_stat — rough approximation
            lines = stdout.decode().strip().split("\n")
            page_size = 16384  # M4 default
            for line in lines:
                if "Pages free" in line:
                    free_pages = int(line.split(":")[1].strip().rstrip("."))
                    free_gb = (free_pages * page_size) / (1024 ** 3)
                    total_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
                    report.memory_used_pct = round((1 - free_gb / total_gb) * 100, 1)
                    if report.memory_used_pct > 97:  # macOS uses memory aggressively via swap; only warn when critical
                        report.warnings.append(
                            f"⚠️ High memory: {report.memory_used_pct}% used"
                        )
                    break
        except Exception:
            pass

        return report

    async def check_and_heal(self) -> HealthReport:
        """Check all services and attempt self-repair on failures."""
        report = await self.check_all()

        for status in report.services:
            if not status.is_running:
                await self._attempt_repair(status.name, report)

        return report

    async def _check_launchagent(
        self, service_id: str, info: Dict[str, Any]
    ) -> ServiceStatus:
        """Check if a LaunchAgent service is running via launchctl."""
        try:
            # Get full launchctl output to extract PID
            proc = await asyncio.create_subprocess_shell(
                f"launchctl list {service_id} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            output = stdout.decode()
            is_running = '"PID"' in output
            pid = ""
            for line in output.splitlines():
                if '"PID"' in line:
                    # Format: "PID" = 12345;
                    pid = line.split("=")[-1].strip().rstrip(";").strip()
                    break
            return ServiceStatus(
                name=info["name"],
                emoji=info["emoji"],
                is_running=is_running,
                details=f"PID {pid}" if is_running else "not running",
            )
        except Exception as e:
            return ServiceStatus(
                name=info["name"],
                emoji=info["emoji"],
                is_running=False,
                error=str(e),
            )

    async def _check_cli(
        self, brain_name: str, info: Dict[str, str]
    ) -> ServiceStatus:
        """Check if a brain CLI is available."""
        # Extend PATH for LaunchAgent environment
        extra_paths = "/opt/homebrew/bin:/usr/local/bin"
        env_path = f"{extra_paths}:{os.environ.get('PATH', '')}"
        cmd_path = shutil.which(info["cmd"], path=env_path)
        return ServiceStatus(
            name=info["name"],
            emoji="🧠",
            is_running=cmd_path is not None,
            details=cmd_path or "not in PATH",
        )

    async def _attempt_repair(
        self, service_name: str, report: HealthReport
    ) -> None:
        """Log failed service — NO auto-restart (causes duplicate instances)."""
        count = self._failure_counts.get(service_name, 0) + 1
        self._failure_counts[service_name] = count

        msg = f"⚠️ {service_name} appears down (check #{count})"
        report.warnings.append(msg)
        logger.warning("watchdog_service_down", service=service_name, count=count)

        # Notify owner once
        if self._notify and service_name not in self._notified:
            self._notified.add(service_name)
            try:
                await self._notify(f"⚠️ {service_name} may be down. Check manually.")
            except Exception:
                pass

    def format_report(self, report: HealthReport) -> str:
        """Format health report as Telegram HTML."""
        lines = ["<b>🏥 AURA Health</b>\n"]

        for s in report.services:
            icon = "✅" if s.is_running else "❌"
            detail = s.details if s.is_running else (s.error or "down")
            lines.append(f"{s.emoji} {icon} {s.name} · {detail}")

        lines.append("")
        lines.append(f"💾 Disk: {report.disk_free_gb}GB free")
        if report.memory_used_pct > 0:
            lines.append(f"🧠 RAM: {report.memory_used_pct}% used")

        if report.warnings:
            lines.append("")
            for w in report.warnings:
                lines.append(w)

        if report.all_healthy:
            lines.append("\n✨ All systems operational.")

        return "\n".join(lines)


# ── Active Telegram ping loop ────────────────────────────────────────────────

_PING_INTERVAL = 120      # seconds between pings
_PING_TIMEOUT = 10        # seconds to wait for getMe
_PING_MAX_FAILURES = 3    # consecutive failures before self-restart
_NOTIFY_CHAT_ID = "854546789"   # Ricardo's Telegram ID


async def _ping_telegram(token: str) -> bool:
    """Return True if Telegram API responds ok:true to getMe."""
    try:
        import json
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=_PING_TIMEOUT) as resp:
            data = json.loads(resp.read())
            return bool(data.get("ok"))
    except Exception as e:
        logger.warning("watchdog_ping_failed", error=str(e)[:120])
        return False


async def _send_restart_notice(token: str, reason: str) -> None:
    """Best-effort Telegram message before self-restart."""
    try:
        import urllib.parse
        import urllib.request
        text = f"⚠️ AURA watchdog auto-restart\nReason: {reason}"
        params = urllib.parse.urlencode({"chat_id": _NOTIFY_CHAT_ID, "text": text})
        url = f"https://api.telegram.org/bot{token}/sendMessage?{params}"
        urllib.request.urlopen(url, timeout=5)
    except Exception:
        pass   # best-effort — Telegram might be down


async def run_ping_loop(token: str) -> None:
    """Active liveness check: ping Telegram every 2 min, auto-restart after 3 failures.

    The LaunchAgent catches crashes but not frozen bots. This handles the frozen case.
    After _PING_MAX_FAILURES consecutive failures: notify Ricardo → SIGTERM self.
    LaunchAgent KeepAlive:true will restart the process cleanly.
    """
    import signal

    failures = 0
    logger.info("watchdog_ping_loop_started",
                interval_s=_PING_INTERVAL, max_failures=_PING_MAX_FAILURES)

    while True:
        await asyncio.sleep(_PING_INTERVAL)

        ok = await _ping_telegram(token)
        if ok:
            if failures > 0:
                logger.info("watchdog_ping_recovered", previous_failures=failures)
            failures = 0
        else:
            failures += 1
            logger.warning("watchdog_ping_failure", consecutive=failures, max=_PING_MAX_FAILURES)

            if failures >= _PING_MAX_FAILURES:
                reason = f"{failures} consecutive getMe failures"
                logger.error("watchdog_triggering_restart", reason=reason)
                try:
                    await asyncio.wait_for(_send_restart_notice(token, reason), timeout=6)
                except Exception:
                    pass
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(5)
                os._exit(1)   # hard exit if SIGTERM didn't propagate
