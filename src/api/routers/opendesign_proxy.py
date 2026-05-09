"""FastAPI proxy router for open-design daemon.

Exposes the daemon API at /api/opendesign/* so the dashboard can call it
without hardcoding the daemon port or dealing with CORS.

Also provides /api/opendesign/status for health checks and port discovery.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from src.services.opendesign_client import OpenDesignClient, discover_daemon_port

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/opendesign", tags=["opendesign"])
_client = OpenDesignClient(timeout=60.0)


@router.get("/status")
async def od_status() -> dict[str, Any]:
    """Check if open-design daemon is running and return port info."""
    port = discover_daemon_port()
    if port is None:
        return {"running": False, "port": None, "web_url": None}
    return {
        "running": True,
        "port": port,
        "daemon_url": f"http://127.0.0.1:{port}",
        "web_url": f"http://127.0.0.1:{port + 2}",
    }


@router.get("/projects")
async def od_list_projects() -> dict[str, Any]:
    """List all open-design projects."""
    try:
        projects = await _client.list_projects()
        return {"projects": projects}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/projects/{project_id}")
async def od_get_project(project_id: str) -> dict[str, Any]:
    """Get a single project."""
    try:
        return await _client.get_project(project_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/projects")
async def od_create_project(request: Request) -> dict[str, Any]:
    """Create a new project."""
    try:
        body = await request.json()
        name = body.get("name", "Untitled")
        skill_id = body.get("skillId", "social-carousel")
        design_system_id = body.get("designSystemId")
        metadata = body.get("metadata")
        return await _client.create_project(
            name=name,
            skill_id=skill_id,
            design_system_id=design_system_id,
            metadata=metadata,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/projects/{project_id}/chat")
async def od_chat(project_id: str, request: Request) -> dict[str, Any]:
    """Send a chat message to a project."""
    try:
        body = await request.json()
        return await _client.send_chat(
            project_id=project_id,
            message=body.get("message", ""),
            agent_id=body.get("agentId", "main"),
            conversation_id=body.get("conversationId"),
            skill_id=body.get("skillId"),
            design_system_id=body.get("designSystemId"),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/projects/{project_id}/artifacts")
async def od_list_artifacts(project_id: str) -> dict[str, Any]:
    """List artifacts for a project."""
    try:
        artifacts = await _client.list_artifacts(project_id)
        return {"artifacts": artifacts}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/skills")
async def od_list_skills() -> dict[str, Any]:
    """List all available skills grouped by surface."""
    try:
        skills = await _client.list_skills()
        by_surface: dict[str, list[Any]] = {}
        for s in skills:
            surface = s.get("surface", "web")
            by_surface.setdefault(surface, []).append(s)
        return {"skills": skills, "by_surface": by_surface}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/design-systems")
async def od_list_design_systems() -> dict[str, Any]:
    """List available design systems."""
    try:
        return await _client._get("/api/design-systems")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def od_proxy(path: str, request: Request) -> Response:
    """Generic proxy — forwards any /api/opendesign/<path> to daemon /api/<path>.

    This is the fallback for any daemon endpoint not explicitly handled above,
    so the dashboard can access the full API without per-route handlers.
    """
    port = discover_daemon_port()
    if port is None:
        raise HTTPException(status_code=503, detail="open-design daemon not running")

    daemon_url = f"http://127.0.0.1:{port}/api/{path}"
    method = request.method
    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(
                method=method,
                url=daemon_url,
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="open-design daemon unreachable")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")
        },
        media_type=resp.headers.get("content-type", "application/json"),
    )
