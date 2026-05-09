"""open-design daemon client with dynamic port discovery.

The open-design daemon runs at a random port that changes on every restart.
Port discovery uses lsof to find the IPv4 listening socket on the node process
whose command line contains 'apps/daemon'.

Usage:
    client = OpenDesignClient()
    port = client.get_daemon_port()       # -> int | None
    projects = await client.list_projects()
    skills = await client.list_skills()
    new_proj = await client.create_project("carousel", "social-carousel")
    status = await client.get_project_status(project_id)
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from functools import lru_cache
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# Port cache TTL — recheck if daemon was restarted
_DAEMON_PORT: Optional[int] = None


def discover_daemon_port() -> Optional[int]:
    """Find the open-design daemon's HTTP port using lsof.

    Looks for a node process with 'apps/daemon' in its command line
    that is listening on an IPv4 TCP socket on localhost.

    Returns the port number, or None if not found.
    """
    global _DAEMON_PORT
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-i", "-P", "-n"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Collect listening IPv4 node PIDs and their ports
        port_by_pid: dict[str, int] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 10:
                continue
            if parts[0] != "node" or "LISTEN" not in parts[-1]:
                continue
            if "IPv4" not in line:
                continue
            # parts[8] is "127.0.0.1:PORT" or "*:PORT"
            addr = parts[8]
            if ":" not in addr:
                continue
            port_str = addr.rsplit(":", 1)[-1]
            if port_str.isdigit():
                port_by_pid[parts[1]] = int(port_str)

        if not port_by_pid:
            return None

        # Find the PID whose command line contains 'apps/daemon'
        ps_result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in ps_result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            pid, args = parts
            if "apps/daemon" in args and pid in port_by_pid:
                _DAEMON_PORT = port_by_pid[pid]
                log.debug("open-design daemon discovered at port %d", _DAEMON_PORT)
                return _DAEMON_PORT

    except Exception as exc:
        log.warning("open-design port discovery failed: %s", exc)

    return None


def get_daemon_base_url() -> Optional[str]:
    """Return the base URL for the daemon API, or None if not running."""
    port = discover_daemon_port()
    if port is None:
        return None
    return f"http://127.0.0.1:{port}"


class OpenDesignClient:
    """Async HTTP client for the open-design daemon API."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def _base_url(self) -> Optional[str]:
        return get_daemon_base_url()

    async def _get(self, path: str) -> Any:
        base = self._base_url()
        if base is None:
            raise RuntimeError("open-design daemon is not running")
        url = f"{base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        base = self._base_url()
        if base is None:
            raise RuntimeError("open-design daemon is not running")
        url = f"{base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()

    async def is_running(self) -> bool:
        """Check if the daemon is reachable."""
        try:
            await self._get("/api/projects")
            return True
        except Exception:
            return False

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return all projects."""
        data = await self._get("/api/projects")
        return data.get("projects", [])

    async def list_skills(self) -> list[dict[str, Any]]:
        """Return all available skills (generation modes)."""
        data = await self._get("/api/skills")
        return data.get("skills", [])

    async def list_skills_by_surface(self) -> dict[str, list[str]]:
        """Return skills grouped by surface (image/video/audio/web)."""
        skills = await self.list_skills()
        grouped: dict[str, list[str]] = {}
        for skill in skills:
            surface = skill.get("surface", "web")
            grouped.setdefault(surface, []).append(skill["id"])
        return grouped

    async def list_design_systems(self) -> list[dict[str, Any]]:
        """Return available design systems."""
        data = await self._get("/api/design-systems")
        return data.get("designSystems", [])

    async def list_connectors(self) -> list[dict[str, Any]]:
        """Return available connectors."""
        data = await self._get("/api/connectors")
        return data.get("connectors", [])

    async def create_project(
        self,
        name: str,
        skill_id: str = "social-carousel",
        design_system_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create a new project and return its data."""
        body: dict[str, Any] = {
            "name": name,
            "skillId": skill_id,
        }
        if design_system_id:
            body["designSystemId"] = design_system_id
        if metadata:
            body["metadata"] = metadata
        return await self._post("/api/projects", body)

    async def get_project(self, project_id: str) -> dict[str, Any]:
        """Get a single project by ID."""
        return await self._get(f"/api/projects/{project_id}")

    async def get_project_status(self, project_id: str) -> str:
        """Return the project display status string."""
        proj = await self.get_project(project_id)
        return proj.get("status", {}).get("value", "unknown")

    async def send_chat(
        self,
        project_id: str,
        message: str,
        agent_id: str = "main",
        conversation_id: Optional[str] = None,
        skill_id: Optional[str] = None,
        design_system_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send a chat message to a project's AI agent (creates a run)."""
        import uuid

        body: dict[str, Any] = {
            "agentId": agent_id,
            "message": message,
            "projectId": project_id,
            "conversationId": conversation_id or str(uuid.uuid4()),
            "assistantMessageId": str(uuid.uuid4()),
            "clientRequestId": str(uuid.uuid4()),
        }
        if skill_id:
            body["skillId"] = skill_id
        if design_system_id:
            body["designSystemId"] = design_system_id
        return await self._post(f"/api/projects/{project_id}/chat/runs", body)

    async def wait_for_project(
        self, project_id: str, poll_interval: float = 2.0, timeout: float = 120.0
    ) -> str:
        """Poll project status until terminal state. Returns final status."""
        terminal = {"succeeded", "failed", "canceled"}
        elapsed = 0.0
        while elapsed < timeout:
            status = await self.get_project_status(project_id)
            if status in terminal:
                return status
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return "timeout"

    async def list_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        """List artifacts for a project."""
        data = await self._get(f"/api/projects/{project_id}/artifacts")
        return data.get("artifacts", [])

    async def get_artifact_url(self, project_id: str, artifact_id: str) -> str:
        """Return the URL to view/embed an artifact."""
        base = self._base_url()
        if base is None:
            raise RuntimeError("open-design daemon is not running")
        return f"{base}/projects/{project_id}/artifacts/{artifact_id}"

    def get_web_url(self) -> Optional[str]:
        """Return the web UI URL (port = daemon port + 2, conventionally)."""
        port = discover_daemon_port()
        if port is None:
            return None
        # The web UI runs 2 ports above the daemon in tools-dev mode
        return f"http://127.0.0.1:{port + 2}"
