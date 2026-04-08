"""Weekly Report — comprehensive summary of the week.

Schedule: Sunday 8:00 PM (0 20 * * 0)
Trigger: /report command or scheduler
Tokens: ZERO (pure data gathering + formatting)
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

_DEFAULT_SCAN_DIRS = [
    Path.home() / "claude-code-telegram",
]


async def _run_cmd(cmd: str, cwd: Optional[Path] = None) -> str:
    """Run shell command, return stdout."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return stdout.decode().strip()
    except Exception:
        return ""


async def _weekly_git_stats(
    scan_dirs: Optional[List[Path]] = None,
) -> List[Dict[str, Any]]:
    """Git stats for the last 7 days across projects."""
    dirs = scan_dirs or _DEFAULT_SCAN_DIRS
    stats: List[Dict[str, Any]] = []

    for project_dir in dirs:
        if not (project_dir / ".git").exists():
            continue

        # Commit count
        count_str = await _run_cmd(
            'git rev-list --count --since="7 days ago" HEAD',
            cwd=project_dir,
        )
        commit_count = int(count_str) if count_str.isdigit() else 0

        # Lines changed
        diff_stat = await _run_cmd(
            'git diff --shortstat "HEAD@{7 days ago}" HEAD 2>/dev/null',
            cwd=project_dir,
        )

        # Top commit messages
        top_commits = await _run_cmd(
            'git log --since="7 days ago" --format="%s" --no-merges | head -5',
            cwd=project_dir,
        )

        if commit_count > 0:
            stats.append({
                "project": project_dir.name,
                "commits": commit_count,
                "diff": diff_stat or "N/A",
                "highlights": [
                    c for c in top_commits.split("\n") if c
                ][:5],
            })

    return stats


async def _brain_weekly_usage() -> Dict[str, Dict[str, int]]:
    """Brain usage stats for the week."""
    usage_file = Path.home() / ".aura" / "usage.json"
    try:
        data = json.loads(usage_file.read_text())
        brains = data.get("brains", {})
        result = {}
        week_ago = (datetime.now() - timedelta(days=7)).timestamp()

        for name, info in brains.items():
            requests = info.get("requests", [])
            errors = info.get("errors", [])
            week_reqs = sum(1 for r in requests if r > week_ago)
            week_errs = sum(1 for e in errors if e > week_ago)
            if week_reqs > 0 or week_errs > 0:
                result[name] = {"requests": week_reqs, "errors": week_errs}

        return result
    except Exception:
        return {}


async def _cache_stats() -> Dict[str, Any]:
    """Response cache performance."""
    cache_db = Path.home() / ".aura" / "cache.db"
    if not cache_db.exists():
        return {}

    try:
        import sqlite3
        conn = sqlite3.connect(str(cache_db))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM response_cache")
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM response_cache WHERE created_at > ?",
            (int((datetime.now() - timedelta(days=7)).timestamp()),),
        )
        week = cursor.fetchone()[0]
        conn.close()
        return {"total_entries": total, "new_this_week": week}
    except Exception:
        return {}


async def generate_weekly_report(
    scan_dirs: Optional[List[Path]] = None,
) -> str:
    """Generate the weekly report.

    Returns a Telegram-formatted message.
    """
    now = datetime.now()
    week_start = (now - timedelta(days=7)).strftime("%d %b")
    header = f"📊 *Weekly Report — {week_start} → {now.strftime('%d %b')}*\n"

    sections: List[str] = [header]

    # 1. Git stats
    git_stats = await _weekly_git_stats(scan_dirs)
    if git_stats:
        total_commits = sum(s["commits"] for s in git_stats)
        git_lines = [f"📝 *Código* — {total_commits} commits total"]
        for stat in git_stats:
            git_lines.append(f"  `{stat['project']}`: {stat['commits']} commits")
            if stat["diff"] and stat["diff"] != "N/A":
                git_lines.append(f"    {stat['diff']}")
            for h in stat["highlights"][:2]:
                git_lines.append(f"    • {h}")
        sections.append("\n".join(git_lines))
    else:
        sections.append("📝 *Código*: Sin actividad esta semana")

    # 2. Brain usage
    brain_usage = await _brain_weekly_usage()
    if brain_usage:
        total_reqs = sum(v["requests"] for v in brain_usage.values())
        total_errs = sum(v["errors"] for v in brain_usage.values())
        brain_lines = [f"🧠 *Brains* — {total_reqs} requests, {total_errs} errors"]
        for name, usage in sorted(
            brain_usage.items(), key=lambda x: x[1]["requests"], reverse=True
        ):
            err_str = f" ({usage['errors']} err)" if usage["errors"] > 0 else ""
            brain_lines.append(f"  • {name}: {usage['requests']} reqs{err_str}")
        sections.append("\n".join(brain_lines))

    # 3. Cache performance
    cache = await _cache_stats()
    if cache:
        sections.append(
            f"💾 *Cache*: {cache.get('total_entries', 0)} entradas, "
            f"{cache.get('new_this_week', 0)} nuevas esta semana"
        )

    # 4. System health
    import shutil
    try:
        disk = shutil.disk_usage(str(Path.home()))
        free_gb = round(disk.free / (1024**3), 1)
        total_gb = round(disk.total / (1024**3), 1)
        sections.append(f"💻 *Sistema*: {free_gb}/{total_gb}GB disco libre")
    except Exception:
        pass

    # 5. Add motivational footer based on activity
    if git_stats:
        total = sum(s["commits"] for s in git_stats)
        if total > 20:
            sections.append("🔥 _Semana productiva. Sigue así._")
        elif total > 5:
            sections.append("👍 _Buen ritmo. Puedes más._")
        else:
            sections.append("💤 _Semana tranquila. ¿Vacaciones?_")

    return "\n\n".join(sections)
