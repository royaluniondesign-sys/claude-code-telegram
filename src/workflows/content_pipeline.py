"""Content Pipeline — morning briefing with git, tasks, and system status.

Schedule: 8:00 AM weekdays (0 8 * * 1-5)
Trigger: /standup command or scheduler
Tokens: ZERO (pure data gathering + formatting)
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Projects to scan for git activity — auto-discovered from ~/.aura/memory/
_DEFAULT_SCAN_DIRS = [
    Path.home() / "claude-code-telegram",
]


async def _run_cmd(cmd: str, cwd: Optional[Path] = None) -> str:
    """Run shell command, return stdout or empty string on error."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return stdout.decode().strip()
    except Exception as e:
        logger.debug("cmd_failed", cmd=cmd[:60], error=str(e))
        return ""


async def _git_activity(scan_dirs: Optional[List[Path]] = None) -> List[Dict[str, Any]]:
    """Get git commits from the last 24 hours across projects."""
    dirs = scan_dirs or _DEFAULT_SCAN_DIRS
    activity: List[Dict[str, Any]] = []

    for project_dir in dirs:
        git_dir = project_dir / ".git"
        if not git_dir.exists():
            continue

        log_output = await _run_cmd(
            'git log --since="24 hours ago" --format="%h|%s|%ar" --no-merges',
            cwd=project_dir,
        )
        if not log_output:
            continue

        commits = []
        for line in log_output.split("\n"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append({
                    "hash": parts[0],
                    "message": parts[1],
                    "time": parts[2],
                })

        if commits:
            activity.append({
                "project": project_dir.name,
                "commits": commits,
            })

    return activity


async def _pending_from_memory() -> List[str]:
    """Extract pending items from AURA memory."""
    memory_file = Path.home() / ".aura" / "memory" / "MEMORY.md"
    try:
        content = memory_file.read_text()
        # Look for TODO/pending items
        pending = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- [ ]") or stripped.startswith("- TODO"):
                pending.append(stripped.lstrip("- [ ]").lstrip("- TODO").strip())
        return pending[:5]  # Top 5
    except Exception as e:
        logger.debug("memory_read_error", error=str(e))
        return []


async def _brain_status() -> Dict[str, str]:
    """Quick brain availability check."""
    usage_file = Path.home() / ".aura" / "usage.json"
    try:
        data = json.loads(usage_file.read_text())
        brains = data.get("brains", {})
        result = {}
        for name, info in brains.items():
            reqs = len(info.get("requests", []))
            result[name] = f"{reqs} requests"
        return result
    except Exception as e:
        logger.debug("brain_status_error", error=str(e))
        return {}


async def _system_health_brief() -> Dict[str, Any]:
    """Minimal system health snapshot."""
    import os
    import shutil

    info: Dict[str, Any] = {}

    # Disk
    try:
        usage = shutil.disk_usage(str(Path.home()))
        free_gb = round(usage.free / (1024**3), 1)
        info["disk_free"] = f"{free_gb}GB"
        info["disk_warning"] = free_gb < 10
    except Exception as e:
        logger.debug("disk_check_error", error=str(e))
        info["disk_free"] = "?"

    # Uptime
    uptime = await _run_cmd("uptime -p 2>/dev/null || uptime")
    info["uptime"] = uptime[:60] if uptime else "?"

    return info


async def generate_standup(
    scan_dirs: Optional[List[Path]] = None,
) -> str:
    """Generate the daily standup report.

    Returns a Telegram-formatted message (Markdown).
    """
    now = datetime.now()
    header = f"🌅 *Content Pipeline — {now.strftime('%A %d %b')}*\n"

    sections: List[str] = [header]

    # 1. Git activity
    activity = await _git_activity(scan_dirs)
    if activity:
        git_lines = ["📝 *Git Activity (24h)*"]
        for project in activity:
            git_lines.append(f"  `{project['project']}` — {len(project['commits'])} commits")
            for c in project["commits"][:3]:
                git_lines.append(f"    • `{c['hash']}` {c['message']}")
            if len(project["commits"]) > 3:
                git_lines.append(f"    _...y {len(project['commits']) - 3} más_")
        sections.append("\n".join(git_lines))
    else:
        sections.append("📝 *Git*: Sin actividad en 24h")

    # 2. Pending items
    pending = await _pending_from_memory()
    if pending:
        pending_lines = ["📋 *Pendientes*"]
        for item in pending:
            pending_lines.append(f"  • {item}")
        sections.append("\n".join(pending_lines))

    # 3. Brain usage
    brains = await _brain_status()
    if brains:
        brain_lines = ["🧠 *Brains (24h)*"]
        for name, usage in brains.items():
            brain_lines.append(f"  • {name}: {usage}")
        sections.append("\n".join(brain_lines))

    # 4. System health
    health = await _system_health_brief()
    health_emoji = "⚠️" if health.get("disk_warning") else "✅"
    sections.append(
        f"{health_emoji} *Sistema*: Disco {health.get('disk_free', '?')} libre"
    )

    return "\n\n".join(sections)
