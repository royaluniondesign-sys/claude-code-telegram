"""Git tools — status, log, commit, push."""
from __future__ import annotations
import asyncio
from pathlib import Path
from src.actions.registry import aura_tool

_DEFAULT_REPO = str(Path.home())


async def _git(args: str, cwd: str = _DEFAULT_REPO) -> str:
    proc = await asyncio.create_subprocess_shell(
        f"git {args}", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=cwd,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    return (out or err).decode().strip()[:2000]


@aura_tool(name="git_status", description="Git status for a repository.", category="git",
           parameters={"repo": {"type": "str", "description": "Repo path (default: home)"}})
async def git_status(repo: str = _DEFAULT_REPO) -> str:
    return await _git("status --short", cwd=repo)


@aura_tool(name="git_log", description="Recent git commits.", category="git",
           parameters={"repo": {"type": "str"}, "n": {"type": "int", "description": "Number of commits"}})
async def git_log(repo: str = _DEFAULT_REPO, n: int = 10) -> str:
    return await _git(f"log --oneline -{n}", cwd=repo)


@aura_tool(name="git_commit", description="Stage all and commit with a message.", category="git",
           parameters={"message": {"type": "str"}, "repo": {"type": "str"}})
async def git_commit(message: str, repo: str = _DEFAULT_REPO) -> str:
    await _git("add -A", cwd=repo)
    return await _git(f'commit -m "{message}"', cwd=repo)
