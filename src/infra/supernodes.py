"""SuperNodes — Distributed capability-aware task execution.

Each machine in the AURA fleet is a SuperNode with a capability profile.
The dispatcher routes tasks to the best node based on:
  - Hardware (RAM, CPU cores, GPU)
  - Available tools (ffmpeg, docker, python, node, etc.)
  - Current load
  - Specialization tags

Architecture:
  Telegram → AURA Primary → Dispatcher → Best SuperNode → Execute → Report back

Example from Telegram:
  /dispatch render "ffmpeg -i input.mp4 -vf scale=1920:1080 output.mp4"
  → Routes to Windows 32GB (has GPU + ffmpeg + most RAM)

  /dispatch code "run pytest on project X"
  → Routes to Mac M4 (best single-thread, has dev tools)

  /dispatch bulk "process 500 images" --parallel
  → Splits across ALL nodes simultaneously
"""

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

from .fleet import FleetManager, Machine, SSHResult

logger = structlog.get_logger()

_NODES_FILE = Path.home() / ".aura" / "supernodes.json"


class TaskType(str, Enum):
    """Task categories for routing."""

    RENDER = "render"      # Video/image processing — needs GPU/RAM
    CODE = "code"          # Development tasks — needs dev tools
    BUILD = "build"        # Compilation — needs CPU cores
    AI = "ai"             # AI inference — needs GPU/VRAM
    DATA = "data"          # Data processing — needs RAM
    NETWORK = "network"    # Downloads, scraping — needs bandwidth
    GENERAL = "general"    # Anything


@dataclass(frozen=True)
class NodeProfile:
    """Capability profile of a SuperNode."""

    machine_name: str
    ram_gb: float
    cpu_cores: int
    has_gpu: bool
    gpu_vram_gb: float
    platform: str  # darwin, linux, windows
    tools: tuple  # ("ffmpeg", "docker", "python", "node", etc.)
    specializations: tuple  # ("render", "code", "build", etc.)
    max_concurrent: int  # Max parallel tasks
    priority: int  # Lower = preferred (0 = primary)

    @property
    def capability_score(self) -> float:
        """Overall capability score (higher = more capable)."""
        score = self.ram_gb * 2
        score += self.cpu_cores * 3
        if self.has_gpu:
            score += self.gpu_vram_gb * 10
        score += len(self.tools) * 1.5
        return score

    def score_for_task(self, task_type: TaskType) -> float:
        """Score this node for a specific task type."""
        base = self.capability_score

        # Boost if task matches specialization
        if task_type.value in self.specializations:
            base *= 2.0

        # Task-specific scoring
        if task_type == TaskType.RENDER:
            base += self.ram_gb * 3  # RAM matters
            if self.has_gpu:
                base += self.gpu_vram_gb * 20  # GPU is king
            if "ffmpeg" in self.tools:
                base += 50

        elif task_type == TaskType.CODE:
            if "python" in self.tools:
                base += 20
            if "node" in self.tools:
                base += 15
            if "docker" in self.tools:
                base += 10

        elif task_type == TaskType.BUILD:
            base += self.cpu_cores * 10  # CPU cores matter most
            base += self.ram_gb * 2

        elif task_type == TaskType.AI:
            if self.has_gpu:
                base += self.gpu_vram_gb * 30
            base += self.ram_gb * 5

        elif task_type == TaskType.DATA:
            base += self.ram_gb * 5

        return base

    def to_dict(self) -> Dict[str, Any]:
        return {
            "machine_name": self.machine_name,
            "ram_gb": self.ram_gb,
            "cpu_cores": self.cpu_cores,
            "has_gpu": self.has_gpu,
            "gpu_vram_gb": self.gpu_vram_gb,
            "platform": self.platform,
            "tools": list(self.tools),
            "specializations": list(self.specializations),
            "max_concurrent": self.max_concurrent,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class DispatchResult:
    """Result of a dispatched task."""

    task_id: str
    task_type: str
    node_name: str
    command: str
    ssh_result: Optional[SSHResult]
    dispatched_at: float
    completed_at: Optional[float]
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        if self.error:
            return False
        if self.ssh_result:
            return self.ssh_result.success
        return False

    @property
    def duration_ms(self) -> int:
        if self.completed_at and self.dispatched_at:
            return int((self.completed_at - self.dispatched_at) * 1000)
        return 0


class SuperNodeManager:
    """Manages SuperNode profiles and task dispatching."""

    def __init__(self, fleet: Optional[FleetManager] = None) -> None:
        self._fleet = fleet or FleetManager()
        self._profiles: Dict[str, NodeProfile] = {}
        self._active_tasks: Dict[str, int] = {}  # node -> current task count
        self._task_counter = 0
        self._load()

    def _load(self) -> None:
        """Load node profiles from disk."""
        try:
            if _NODES_FILE.exists():
                data = json.loads(_NODES_FILE.read_text())
                for entry in data.get("nodes", []):
                    profile = NodeProfile(
                        machine_name=entry["machine_name"],
                        ram_gb=entry.get("ram_gb", 8),
                        cpu_cores=entry.get("cpu_cores", 4),
                        has_gpu=entry.get("has_gpu", False),
                        gpu_vram_gb=entry.get("gpu_vram_gb", 0),
                        platform=entry.get("platform", "linux"),
                        tools=tuple(entry.get("tools", [])),
                        specializations=tuple(entry.get("specializations", [])),
                        max_concurrent=entry.get("max_concurrent", 2),
                        priority=entry.get("priority", 10),
                    )
                    self._profiles[profile.machine_name] = profile
                logger.info("supernodes_loaded", count=len(self._profiles))
        except Exception as e:
            logger.warning("supernodes_load_error", error=str(e))

    def _save(self) -> None:
        """Persist node profiles."""
        try:
            _NODES_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "nodes": [p.to_dict() for p in self._profiles.values()],
                "updated_at": time.time(),
            }
            _NODES_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("supernodes_save_error", error=str(e))

    def register_node(
        self,
        machine_name: str,
        ram_gb: float = 8,
        cpu_cores: int = 4,
        has_gpu: bool = False,
        gpu_vram_gb: float = 0,
        platform: str = "linux",
        tools: tuple = (),
        specializations: tuple = (),
        max_concurrent: int = 2,
        priority: int = 10,
    ) -> NodeProfile:
        """Register or update a SuperNode profile."""
        profile = NodeProfile(
            machine_name=machine_name,
            ram_gb=ram_gb,
            cpu_cores=cpu_cores,
            has_gpu=has_gpu,
            gpu_vram_gb=gpu_vram_gb,
            platform=platform,
            tools=tools,
            specializations=specializations,
            max_concurrent=max_concurrent,
            priority=priority,
        )
        self._profiles[machine_name] = profile
        self._save()
        logger.info("supernode_registered", name=machine_name, score=profile.capability_score)
        return profile

    async def auto_profile(self, machine_name: str) -> Optional[NodeProfile]:
        """Auto-detect capabilities of a machine via SSH."""
        detect_script = (
            "echo RAM_GB=$(("
            "$(if [ -f /proc/meminfo ]; then "
            "grep MemTotal /proc/meminfo | awk '{print $2}'; "
            "else sysctl -n hw.memsize 2>/dev/null | awk '{print $1/1024}'; fi"
            ") / 1024 / 1024)); "
            "echo CPU_CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4); "
            "echo PLATFORM=$(uname -s | tr '[:upper:]' '[:lower:]'); "
            "echo HAS_GPU=$(nvidia-smi >/dev/null 2>&1 && echo true || echo false); "
            "for tool in ffmpeg docker python3 node git gcc cargo go java ruby; do "
            "command -v $tool >/dev/null 2>&1 && echo TOOL=$tool; done"
        )

        result = await self._fleet.execute(machine_name, detect_script, timeout=15)
        if not result.success:
            logger.warning("auto_profile_failed", node=machine_name, error=result.output)
            return None

        # Parse output
        ram_gb = 8.0
        cpu_cores = 4
        platform = "linux"
        has_gpu = False
        tools = []

        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("RAM_GB="):
                try:
                    ram_gb = float(line.split("=")[1])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("CPU_CORES="):
                try:
                    cpu_cores = int(line.split("=")[1])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("PLATFORM="):
                platform = line.split("=")[1].strip()
                if platform == "darwin":
                    platform = "darwin"
                elif "mingw" in platform or "msys" in platform:
                    platform = "windows"
            elif line.startswith("HAS_GPU="):
                has_gpu = line.split("=")[1].strip() == "true"
            elif line.startswith("TOOL="):
                tools.append(line.split("=")[1].strip())

        # Auto-assign specializations
        specs = []
        if has_gpu or ram_gb >= 24:
            specs.append("render")
        if "python3" in tools or "node" in tools:
            specs.append("code")
        if "docker" in tools:
            specs.append("build")
        if "ffmpeg" in tools:
            specs.append("render")
        if ram_gb >= 16:
            specs.append("data")

        machine = self._fleet.get_machine(machine_name)
        if machine:
            platform = machine.platform

        return self.register_node(
            machine_name=machine_name,
            ram_gb=ram_gb,
            cpu_cores=cpu_cores,
            has_gpu=has_gpu,
            platform=platform,
            tools=tuple(tools),
            specializations=tuple(set(specs)),
            max_concurrent=max(1, cpu_cores // 2),
            priority=0 if machine_name == "mac-m4" else 10,
        )

    def best_node_for(self, task_type: TaskType) -> Optional[str]:
        """Find the best available node for a task type."""
        if not self._profiles:
            return None

        candidates: List[Tuple[float, str]] = []
        for name, profile in self._profiles.items():
            # Check if node has capacity
            active = self._active_tasks.get(name, 0)
            if active >= profile.max_concurrent:
                continue

            # Check if machine is in fleet and reachable
            machine = self._fleet.get_machine(name)
            if not machine:
                continue

            score = profile.score_for_task(task_type)
            # Penalize by current load
            score *= 1.0 - (active / max(1, profile.max_concurrent)) * 0.5
            candidates.append((score, name))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        return candidates[0][1]

    async def dispatch(
        self,
        command: str,
        task_type: TaskType = TaskType.GENERAL,
        target_node: Optional[str] = None,
        timeout: int = 300,
    ) -> DispatchResult:
        """Dispatch a task to the best (or specified) node."""
        self._task_counter += 1
        task_id = f"task-{self._task_counter}-{int(time.time())}"
        dispatched_at = time.time()

        # Pick node
        node_name = target_node or self.best_node_for(task_type)
        if not node_name:
            return DispatchResult(
                task_id=task_id,
                task_type=task_type.value,
                node_name="none",
                command=command,
                ssh_result=None,
                dispatched_at=dispatched_at,
                completed_at=time.time(),
                error="No available node for this task type.",
            )

        # Track active task
        self._active_tasks[node_name] = self._active_tasks.get(node_name, 0) + 1

        try:
            logger.info(
                "task_dispatched",
                task_id=task_id,
                node=node_name,
                type=task_type.value,
                command=command[:100],
            )

            ssh_result = await self._fleet.execute(node_name, command, timeout=timeout)

            return DispatchResult(
                task_id=task_id,
                task_type=task_type.value,
                node_name=node_name,
                command=command,
                ssh_result=ssh_result,
                dispatched_at=dispatched_at,
                completed_at=time.time(),
            )
        except Exception as e:
            return DispatchResult(
                task_id=task_id,
                task_type=task_type.value,
                node_name=node_name,
                command=command,
                ssh_result=None,
                dispatched_at=dispatched_at,
                completed_at=time.time(),
                error=str(e),
            )
        finally:
            self._active_tasks[node_name] = max(
                0, self._active_tasks.get(node_name, 1) - 1
            )

    async def dispatch_parallel(
        self,
        commands: List[str],
        task_type: TaskType = TaskType.GENERAL,
        timeout: int = 300,
    ) -> List[DispatchResult]:
        """Dispatch multiple tasks in parallel across nodes."""
        tasks = []
        for cmd in commands:
            tasks.append(
                asyncio.create_task(
                    self.dispatch(cmd, task_type=task_type, timeout=timeout)
                )
            )
        return await asyncio.gather(*tasks)

    def get_profile(self, machine_name: str) -> Optional[NodeProfile]:
        """Get a node's profile."""
        return self._profiles.get(machine_name)

    def list_profiles(self) -> List[NodeProfile]:
        """List all node profiles."""
        return sorted(
            self._profiles.values(),
            key=lambda p: p.capability_score,
            reverse=True,
        )

    def format_nodes_status(self) -> str:
        """Format all nodes as Telegram HTML."""
        if not self._profiles:
            return (
                "<b>🌐 SuperNodes</b>\n\n"
                "No nodes profiled yet.\n"
                "Use <code>/nodes profile machine-name</code> to auto-detect."
            )

        lines = ["<b>🌐 AURA SuperNodes</b>\n"]

        for p in self.list_profiles():
            machine = self._fleet.get_machine(p.machine_name)
            online = "🟢" if (machine and machine.is_reachable) else "⚪"
            platform_icon = {
                "darwin": "🍎", "linux": "🐧", "windows": "🪟"
            }.get(p.platform, "💻")

            gpu_str = f"GPU {p.gpu_vram_gb}GB" if p.has_gpu else "no GPU"
            active = self._active_tasks.get(p.machine_name, 0)

            lines.append(
                f"{online} {platform_icon} <b>{p.machine_name}</b> "
                f"· score: {int(p.capability_score)}"
            )
            lines.append(
                f"   {p.ram_gb}GB RAM · {p.cpu_cores} cores · {gpu_str}"
            )
            lines.append(
                f"   Tools: {', '.join(p.tools[:8]) or 'none detected'}"
            )
            if p.specializations:
                lines.append(
                    f"   Specializations: {', '.join(p.specializations)}"
                )
            lines.append(
                f"   Load: {active}/{p.max_concurrent} tasks"
            )
            lines.append("")

        lines.append(
            "💡 <code>/dispatch [type] command</code> — auto-routes to best node\n"
            "Types: render, code, build, ai, data, general"
        )
        return "\n".join(lines)
