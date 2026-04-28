"""AURA self-status tool — gathers real system + bot state."""
from __future__ import annotations
import asyncio
import os
import time
from pathlib import Path
from src.actions.registry import aura_tool


@aura_tool(
    name="get_aura_status",
    description="Get AURA's current operational status: brains, rate limits, memory, disk, errors.",
    category="system",
    parameters={},
)
async def get_aura_status() -> str:
    lines: list[str] = []

    # Bot process
    try:
        proc = await asyncio.create_subprocess_shell(
            "launchctl list com.aura.telegram-bot 2>/dev/null | awk '{print $1,$3}'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        lines.append(f"Bot: {out.decode().strip() or 'unknown'}")
    except Exception:
        lines.append("Bot: check failed")

    # Disk
    try:
        import shutil
        usage = shutil.disk_usage("/")
        pct = usage.used / usage.total * 100
        free_gb = usage.free / 1e9
        lines.append(f"Disk: {pct:.0f}% used · {free_gb:.1f}GB free")
    except Exception:
        pass

    # Memory (using top PhysMem for accurate calculation including compressor)
    try:
        proc = await asyncio.create_subprocess_shell(
            "top -l 1 | grep PhysMem",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        # Output: "PhysMem: 15G used (2140M wired, 6337M compressor), 335M unused"
        physmem = out.decode().strip()

        # Extract used GB and unused MB
        import re
        used_match = re.search(r'PhysMem: (\d+)G used', physmem)
        unused_match = re.search(r'(\d+)M unused', physmem)

        if used_match and unused_match:
            used_gb = int(used_match.group(1))
            unused_mb = int(unused_match.group(1))
            # Calculate total from OS-reported values (used + unused)
            total_gb = used_gb + (unused_mb / 1024)
            used_pct = (used_gb / total_gb) * 100
            free_gb = unused_mb / 1024
            lines.append(f"RAM: {used_pct:.0f}% used · {free_gb:.1f}GB free")
        else:
            lines.append(f"RAM: parse error")
    except Exception:
        pass

    # Recent errors in log
    log_path = Path.home() / "claude-code-telegram/logs/bot.stdout.log"
    if log_path.exists():
        try:
            import subprocess
            result = subprocess.run(
                ["grep", "-c", '"level": "error"', str(log_path)],
                capture_output=True, text=True, timeout=3,
            )
            err_count = result.stdout.strip()
            lines.append(f"Log errors: {err_count} total")
        except Exception:
            pass

    # Rate limits — RateMonitor.get_all_usage() returns List[BrainUsage]
    try:
        from src.infra.rate_monitor import RateMonitor
        monitor = RateMonitor()
        usages = monitor.get_all_usage()
        if isinstance(usages, dict):
            usages = list(usages.values())
        for usage in usages:
            bar = usage.usage_bar(width=6)
            lines.append(f"  {usage.brain_name}: {bar}")
    except Exception as _re:
        lines.append(f"Rate limits: unavailable ({_re})")

    return "\n".join(lines)
