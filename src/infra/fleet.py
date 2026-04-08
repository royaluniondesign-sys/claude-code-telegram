"""Fleet Manager — multi-machine orchestration for AURA.

Manages remote machines via SSH. Allows AURA to execute commands
on any registered machine from Telegram.

Machines are registered in ~/.aura/fleet.json.
Memory is shared via git sync of ~/.aura/memory/.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

_FLEET_FILE = Path.home() / ".aura" / "fleet.json"
_MEMORY_DIR = Path.home() / ".aura" / "memory"

# SSH timeout for commands
_SSH_TIMEOUT = 60
_CONNECT_TIMEOUT = 10


@dataclass(frozen=True)
class Machine:
    """A registered machine in the fleet."""

    name: str
    host: str  # user@host or SSH alias
    label: str  # Human label (e.g., "Mac Studio", "Dev Server")
    platform: str  # darwin, linux, windows
    added_at: float
    last_seen: Optional[float] = None
    last_error: Optional[str] = None
    tags: tuple = ()  # ("primary", "gpu", "build", etc.)

    @property
    def display(self) -> str:
        icon = {"darwin": "🍎", "linux": "🐧", "windows": "🪟"}.get(
            self.platform, "💻"
        )
        status = "🟢" if self.is_reachable else "⚪"
        return f"{status} {icon} {self.label} ({self.name})"

    @property
    def is_reachable(self) -> bool:
        if self.last_seen is None:
            return False
        return (time.time() - self.last_seen) < 600  # 10 min

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "label": self.label,
            "platform": self.platform,
            "added_at": self.added_at,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class SSHResult:
    """Result of an SSH command execution."""

    machine_name: str
    command: str
    stdout: str
    stderr: str
    return_code: int
    duration_ms: int

    @property
    def success(self) -> bool:
        return self.return_code == 0

    @property
    def output(self) -> str:
        if self.success:
            return self.stdout.strip() or "(no output)"
        return self.stderr.strip() or self.stdout.strip() or f"Exit code {self.return_code}"


class FleetManager:
    """Manages a fleet of machines reachable via SSH."""

    def __init__(self) -> None:
        self._machines: Dict[str, Machine] = {}
        self._load()

    def _load(self) -> None:
        """Load fleet config from disk."""
        try:
            if _FLEET_FILE.exists():
                data = json.loads(_FLEET_FILE.read_text())
                for entry in data.get("machines", []):
                    machine = Machine(
                        name=entry["name"],
                        host=entry["host"],
                        label=entry.get("label", entry["name"]),
                        platform=entry.get("platform", "linux"),
                        added_at=entry.get("added_at", time.time()),
                        last_seen=entry.get("last_seen"),
                        last_error=entry.get("last_error"),
                        tags=tuple(entry.get("tags", [])),
                    )
                    self._machines[machine.name] = machine
                logger.info("fleet_loaded", count=len(self._machines))
        except Exception as e:
            logger.warning("fleet_load_error", error=str(e))

    def _save(self) -> None:
        """Persist fleet config."""
        try:
            _FLEET_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "machines": [m.to_dict() for m in self._machines.values()],
                "updated_at": time.time(),
            }
            _FLEET_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("fleet_save_error", error=str(e))

    def add_machine(
        self,
        name: str,
        host: str,
        label: str = "",
        platform: str = "linux",
        tags: tuple = (),
    ) -> Machine:
        """Register a new machine."""
        machine = Machine(
            name=name,
            host=host,
            label=label or name,
            platform=platform,
            added_at=time.time(),
            tags=tags,
        )
        self._machines[name] = machine
        self._save()
        logger.info("fleet_machine_added", name=name, host=host)
        return machine

    def remove_machine(self, name: str) -> bool:
        """Unregister a machine."""
        if name in self._machines:
            del self._machines[name]
            self._save()
            logger.info("fleet_machine_removed", name=name)
            return True
        return False

    def get_machine(self, name: str) -> Optional[Machine]:
        """Get a machine by name."""
        return self._machines.get(name)

    def list_machines(self) -> List[Machine]:
        """Get all registered machines."""
        return list(self._machines.values())

    def _update_machine(self, name: str, **kwargs: Any) -> None:
        """Update machine fields (creates new immutable instance)."""
        old = self._machines.get(name)
        if not old:
            return
        fields = old.to_dict()
        fields.update(kwargs)
        fields["tags"] = tuple(fields.get("tags", []))
        self._machines[name] = Machine(**fields)
        self._save()

    async def ping(self, name: str) -> bool:
        """Check if a machine is reachable via SSH."""
        machine = self._machines.get(name)
        if not machine:
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
                "-o", "StrictHostKeyChecking=accept-new",
                machine.host,
                "echo ok",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CONNECT_TIMEOUT + 5
            )

            reachable = proc.returncode == 0
            self._update_machine(
                name,
                last_seen=time.time() if reachable else machine.last_seen,
                last_error=None if reachable else stderr.decode().strip()[:200],
            )
            return reachable

        except asyncio.TimeoutError:
            self._update_machine(name, last_error="Connection timed out")
            return False
        except Exception as e:
            self._update_machine(name, last_error=str(e)[:200])
            return False

    async def ping_all(self) -> Dict[str, bool]:
        """Ping all machines concurrently."""
        tasks = {
            name: asyncio.create_task(self.ping(name))
            for name in self._machines
        }
        results = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except Exception:
                results[name] = False
        return results

    async def execute(
        self,
        name: str,
        command: str,
        timeout: int = _SSH_TIMEOUT,
    ) -> SSHResult:
        """Execute a command on a remote machine via SSH."""
        machine = self._machines.get(name)
        if not machine:
            return SSHResult(
                machine_name=name,
                command=command,
                stdout="",
                stderr=f"Machine '{name}' not found in fleet.",
                return_code=1,
                duration_ms=0,
            )

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
                "-o", "StrictHostKeyChecking=accept-new",
                machine.host,
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed_ms = int((time.time() - start) * 1000)

            self._update_machine(name, last_seen=time.time(), last_error=None)

            return SSHResult(
                machine_name=name,
                command=command,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
                return_code=proc.returncode or 0,
                duration_ms=elapsed_ms,
            )

        except asyncio.TimeoutError:
            elapsed_ms = int((time.time() - start) * 1000)
            self._update_machine(name, last_error=f"Command timed out ({timeout}s)")
            return SSHResult(
                machine_name=name,
                command=command,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                return_code=-1,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            self._update_machine(name, last_error=str(e)[:200])
            return SSHResult(
                machine_name=name,
                command=command,
                stdout="",
                stderr=str(e),
                return_code=-1,
                duration_ms=elapsed_ms,
            )

    def format_fleet_status(self) -> str:
        """Format fleet status as Telegram HTML."""
        if not self._machines:
            return (
                "<b>🖥️ Fleet</b>\n\n"
                "No machines registered.\n"
                "Add one: <code>/fleet add name user@host</code>"
            )

        lines = ["<b>🖥️ AURA Fleet</b>\n"]
        for m in self._machines.values():
            lines.append(m.display)
            if m.last_seen:
                ago = int(time.time() - m.last_seen)
                if ago < 60:
                    seen = f"{ago}s ago"
                elif ago < 3600:
                    seen = f"{ago // 60}m ago"
                else:
                    seen = f"{ago // 3600}h ago"
                lines.append(f"   └ Last seen: {seen}")
            if m.last_error:
                lines.append(f"   └ ⚠️ {m.last_error[:80]}")
            if m.tags:
                lines.append(f"   └ Tags: {', '.join(m.tags)}")

        lines.append(f"\nTotal: {len(self._machines)} machines")
        return "\n".join(lines)


class MemorySync:
    """Syncs AURA memory across machines via git."""

    def __init__(self, memory_dir: Path = _MEMORY_DIR) -> None:
        self._dir = memory_dir

    @property
    def is_git_repo(self) -> bool:
        return (self._dir / ".git").exists()

    async def init_repo(self, remote_url: str) -> bool:
        """Initialize memory as a git repo with remote."""
        if self.is_git_repo:
            return True

        try:
            self._dir.mkdir(parents=True, exist_ok=True)

            for cmd in [
                ["git", "init"],
                ["git", "remote", "add", "origin", remote_url],
                ["git", "add", "-A"],
                ["git", "commit", "-m", "Initial AURA memory"],
            ]:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(self._dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)

            logger.info("memory_git_initialized", remote=remote_url)
            return True
        except Exception as e:
            logger.error("memory_git_init_error", error=str(e))
            return False

    async def sync(self) -> Dict[str, Any]:
        """Pull then push — bidirectional sync."""
        if not self.is_git_repo:
            return {"success": False, "error": "Not a git repo"}

        results = {"success": True, "pulled": False, "pushed": False}

        try:
            # Auto-commit any local changes
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=str(self._dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if stdout.decode().strip():
                for cmd in [
                    ["git", "add", "-A"],
                    ["git", "commit", "-m", f"AURA memory sync {int(time.time())}"],
                ]:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(self._dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=15)

            # Pull with rebase
            proc = await asyncio.create_subprocess_exec(
                "git", "pull", "--rebase", "origin", "main",
                cwd=str(self._dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            results["pulled"] = proc.returncode == 0

            # Push
            proc = await asyncio.create_subprocess_exec(
                "git", "push", "origin", "main",
                cwd=str(self._dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            results["pushed"] = proc.returncode == 0

            return results

        except asyncio.TimeoutError:
            return {"success": False, "error": "Sync timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def format_status(self) -> str:
        """Format memory sync status."""
        if not self.is_git_repo:
            return "Memory not synced (no git repo). Use /fleet sync-init <remote>"

        lines = ["<b>🧠 Memory Sync</b>\n"]
        lines.append(f"📂 {self._dir}")
        lines.append(f"🔗 Git: {'✅ initialized' if self.is_git_repo else '❌ not initialized'}")
        return "\n".join(lines)
