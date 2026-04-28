"""Agent Mesh API — endpoints for Hermes ↔ AURA bidirectional communication.

Hermes (OpenClaw) calls:
  POST /api/agent-query   — delegate a task to AURA, get result back
  GET  /api/agent-status  — full AURA state for Hermes context

AURA internals call:
  POST /api/project/update — write progress to shared project folder

Dashboard calls:
  GET  /api/hermes         — Hermes health + skills + sessions + mesh log
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()
router = APIRouter()

_MESH_LOG = Path.home() / ".aura" / "memory" / "mesh-log.md"
_PROJECTS_DIR = Path.home() / ".aura" / "projects"
_MEMORY_DIR = Path.home() / ".aura" / "memory"


def _append_mesh_log(entry: str) -> None:
    try:
        _MESH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_MESH_LOG, "a") as f:
            f.write(f"\n{entry}\n")
    except Exception:
        pass


# ── POST /api/agent-query ─────────────────────────────────────────────────────

@router.post("/api/agent-query")
async def agent_query(request: Request) -> Dict[str, Any]:
    """Hermes delegates a task to AURA. AURA runs it through brain router and returns result."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    task: str = body.get("task", "").strip()
    from_agent: str = body.get("from", "hermes")
    prefer_brain: str = body.get("prefer_brain", "auto")
    project_id: str = body.get("project_id", "")
    write_to: str = body.get("write_to", "")

    if not task:
        return {"ok": False, "error": "task is required"}

    logger.info("agent_mesh_query", from_agent=from_agent, task_preview=task[:80])
    start = time.time()

    # Route through AURA's brain router
    brain_used = "unknown"
    result = ""
    try:
        brain_router = request.app.state.brain_router if hasattr(request.app.state, "brain_router") else None

        if brain_router:
            # Map prefer_brain hint to actual brain
            brain_map = {
                "claude": "haiku",
                "haiku": "haiku",
                "sonnet": "sonnet",
                "opus": "opus",
                "gemini": "gemini",
                "auto": None,
            }
            brain_name = brain_map.get(prefer_brain)

            if brain_name:
                brain = brain_router.get_brain(brain_name)
            else:
                brain_name, _ = brain_router.smart_route(task)
                brain = brain_router.get_brain(brain_name)

            if brain:
                response = await brain.execute(
                    prompt=f"[Tarea delegada por Hermes]\n\n{task}",
                    working_directory=str(Path.home()),
                )
                result = response.content
                brain_used = brain_name
            else:
                result = f"Brain '{brain_name}' not available"
        else:
            # Fallback: use claude CLI directly
            import asyncio
            proc = await asyncio.create_subprocess_exec(
                "/Users/oxyzen/.local/bin/claude", "-p",
                f"[Tarea delegada por Hermes]\n\n{task}",
                "--model", "claude-haiku-4-5-20251001",
                "--output-format", "text",
                "--no-session-persistence",
                "--dangerously-skip-permissions",
                "--setting-sources", "",
                "--max-turns", "10",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            result = stdout.decode("utf-8", errors="replace").strip()
            brain_used = "claude-haiku"

    except Exception as e:
        logger.error("agent_mesh_query_error", error=str(e))
        result = f"Error ejecutando tarea: {str(e)[:200]}"
        return {"ok": False, "error": result}

    elapsed_ms = int((time.time() - start) * 1000)

    # Write to project file if requested
    if project_id and write_to:
        try:
            proj_dir = _PROJECTS_DIR / project_id
            proj_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            (proj_dir / write_to).write_text(
                f"# AURA Progress — {ts}\n\n**Tarea:** {task}\n\n**Resultado:**\n\n{result}\n"
            )
        except Exception as e:
            logger.warning("project_write_error", error=str(e))

    # Append to mesh log
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    _append_mesh_log(
        f"[{ts}] {from_agent.upper()}→AURA: {task[:80]} → brain={brain_used} ({elapsed_ms}ms)"
    )

    return {
        "ok": True,
        "result": result,
        "brain_used": brain_used,
        "elapsed_ms": elapsed_ms,
    }


# ── GET /api/agent-status ─────────────────────────────────────────────────────

@router.get("/api/agent-status")
async def agent_status(request: Request) -> Dict[str, Any]:
    """Return full AURA state for Hermes context awareness."""

    # Brain router status
    brain_info: Dict[str, Any] = {}
    try:
        brain_router = request.app.state.brain_router if hasattr(request.app.state, "brain_router") else None
        if brain_router:
            brain_info = {
                "active": brain_router.active_brain_name,
                "available": brain_router.available_brains,
            }
    except Exception:
        pass

    # Social queue / recent posts
    social_info: Dict[str, Any] = {}
    try:
        hist_file = Path.home() / ".aura" / "social_history.json"
        if hist_file.exists():
            data = json.loads(hist_file.read_text())
            posts = data.get("posts", [])
            social_info = {
                "total_posts": len(posts),
                "last_posts": [
                    {"text": p.get("text", "")[:60], "status": p.get("status"), "ts": p.get("ts")}
                    for p in list(reversed(posts))[:5]
                ],
            }
        drafts_dir = Path.home() / ".aura" / "social_drafts"
        if drafts_dir.exists():
            social_info["drafts_count"] = len(list(drafts_dir.glob("*.jpg")) + list(drafts_dir.glob("*.png")))
    except Exception:
        pass

    # Memory files
    memory_info: Dict[str, Any] = {}
    try:
        if _MEMORY_DIR.exists():
            files = []
            for f in _MEMORY_DIR.glob("*.md"):
                stat = f.stat()
                files.append({
                    "file": f.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%d %H:%M"),
                })
            memory_info["files"] = sorted(files, key=lambda x: x["modified"], reverse=True)
    except Exception:
        pass

    # Social roadmap backlog
    roadmap: Dict[str, Any] = {}
    try:
        roadmap_file = _MEMORY_DIR / "social-roadmap.md"
        if roadmap_file.exists():
            content = roadmap_file.read_text()
            # Extract pending items (lines with [ ] checkbox)
            pending = [l.strip() for l in content.splitlines() if "[ ]" in l][:5]
            done = [l.strip() for l in content.splitlines() if "[x]" in l.lower()][:3]
            roadmap = {"pending": pending, "done_recent": done}
    except Exception:
        pass

    # Dashboard + Termora URLs
    dashboard_url = "http://localhost:4030"
    termora_url = ""
    try:
        import subprocess
        info = subprocess.run(
            ["curl", "-sf", "http://localhost:4030/api/info"],
            capture_output=True, text=True, timeout=2,
        )
        if info.returncode == 0:
            d = json.loads(info.stdout)
            termora_url = d.get("authUrl", d.get("tunnelUrl", ""))
    except Exception:
        pass

    # Active projects
    projects: list = []
    try:
        if _PROJECTS_DIR.exists():
            for p in _PROJECTS_DIR.iterdir():
                if p.is_dir():
                    plan = (p / "plan.md").exists()
                    aura_done = (p / "aura-progress.md").exists()
                    hermes_done = (p / "hermes-progress.md").exists()
                    projects.append({
                        "slug": p.name,
                        "has_plan": plan,
                        "aura_done": aura_done,
                        "hermes_done": hermes_done,
                    })
    except Exception:
        pass

    return {
        "ok": True,
        "agent": "AURA",
        "brain": brain_info,
        "social": social_info,
        "memory": memory_info,
        "roadmap": roadmap,
        "projects": projects,
        "dashboard_url": dashboard_url,
        "termora_url": termora_url,
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ── POST /api/project/update ──────────────────────────────────────────────────

@router.post("/api/project/update")
async def project_update(request: Request) -> Dict[str, Any]:
    """AURA writes progress to a shared project folder."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    project_id: str = body.get("project_id", "").strip()
    content: str = body.get("content", "").strip()
    filename: str = body.get("filename", "aura-progress.md").strip()

    if not project_id or not content:
        return {"ok": False, "error": "project_id and content required"}

    try:
        proj_dir = _PROJECTS_DIR / project_id
        proj_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        (proj_dir / filename).write_text(
            f"# AURA Progress — {ts}\n\n{content}\n"
        )
        logger.info("project_update_ok", project=project_id, file=filename)
        return {"ok": True, "project_id": project_id, "file": filename}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── GET /api/hermes ───────────────────────────────────────────────────────────

_OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
_OPENCLAW_SESSIONS = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
_OPENCLAW_WORKSPACE = Path.home() / ".openclaw" / "workspace"


async def _run(cmd: List[str], timeout: int = 8) -> str:
    """Run a subprocess and return stdout, empty string on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


@router.get("/api/hermes")
async def hermes_status() -> Dict[str, Any]:
    """Return Hermes (OpenClaw) health + skills + sessions + mesh log for the dashboard."""

    # ── Health ─────────────────────────────────────────────────────────────
    health_raw = await _run([_OPENCLAW_BIN, "health"])
    online = bool(health_raw and "ok" in health_raw.lower())
    telegram_ok = "telegram: ok" in health_raw.lower() if health_raw else False

    # Parse model from health output (line like "Model: nvidia/...")
    active_model = "?"
    for line in health_raw.splitlines():
        low = line.lower()
        if "model" in low and ":" in line:
            active_model = line.split(":", 1)[-1].strip()
            break

    # ── Skills ─────────────────────────────────────────────────────────────
    skills_raw = await _run([_OPENCLAW_BIN, "skills", "list"], timeout=6)
    skills_ready: List[str] = []
    skills_missing: List[str] = []
    for line in skills_raw.splitlines():
        if "✓" in line or "ready" in line.lower():
            # Extract skill name (second column)
            parts = line.strip().split()
            name = next((p for p in parts if p and not p.startswith("✓") and p != "ready"), None)
            if name:
                skills_ready.append(name)
        elif "✗" in line or "missing" in line.lower():
            parts = line.strip().split()
            name = next((p for p in parts if p and "✗" not in p and p != "missing"), None)
            if name:
                skills_missing.append(name)

    # ── Sessions ───────────────────────────────────────────────────────────
    sessions: List[Dict[str, Any]] = []
    try:
        if _OPENCLAW_SESSIONS.exists():
            raw = json.loads(_OPENCLAW_SESSIONS.read_text())
            # sessions.json is a dict or list depending on version
            items = raw if isinstance(raw, list) else list(raw.values()) if isinstance(raw, dict) else []
            for s in items[:10]:
                if isinstance(s, dict):
                    sessions.append({
                        "id": s.get("id") or s.get("sessionId", "?")[:16],
                        "ts": s.get("updatedAt") or s.get("createdAt") or s.get("ts"),
                        "preview": (s.get("lastMessage") or s.get("summary") or "")[:80],
                    })
    except Exception:
        pass

    # ── Mesh log ───────────────────────────────────────────────────────────
    mesh_entries: List[str] = []
    try:
        if _MESH_LOG.exists():
            lines = [l.strip() for l in _MESH_LOG.read_text().splitlines() if l.strip()]
            mesh_entries = lines[-10:]
    except Exception:
        pass

    # ── Active projects ────────────────────────────────────────────────────
    projects: List[Dict[str, Any]] = []
    try:
        if _PROJECTS_DIR.exists():
            for p in sorted(_PROJECTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:6]:
                if p.is_dir():
                    projects.append({
                        "slug": p.name,
                        "has_plan": (p / "plan.md").exists(),
                        "hermes_done": (p / "hermes-progress.md").exists(),
                        "aura_done": (p / "aura-progress.md").exists(),
                    })
    except Exception:
        pass

    # ── Workspace files ────────────────────────────────────────────────────
    workspace_files: List[str] = []
    try:
        workspace_files = [f.name for f in _OPENCLAW_WORKSPACE.iterdir()
                           if f.is_file() and f.suffix in (".md", ".json", ".txt")]
    except Exception:
        pass

    return {
        "ok": True,
        "online": online,
        "telegram_ok": telegram_ok,
        "active_model": active_model,
        "skills": {
            "ready": skills_ready,
            "missing_count": len(skills_missing),
            "ready_count": len(skills_ready),
        },
        "sessions": sessions,
        "mesh_log": mesh_entries,
        "projects": projects,
        "workspace_files": workspace_files,
        "gateway_port": 18789,
        "timestamp": datetime.now(UTC).isoformat(),
    }
