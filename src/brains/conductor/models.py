"""Conductor data models: ConductorStep, ConductorPlan, ConductorResult."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ConductorStep:
    """One step in the execution plan."""
    step: int
    layer: int
    brain: str
    role: str
    prompt: str
    depends_on: List[int] = field(default_factory=list)
    # Runtime fields
    status: str = "pending"       # pending | running | done | failed
    output: str = ""
    duration_ms: int = 0
    error: str = ""


@dataclass
class ConductorPlan:
    """Full execution plan returned by Claude."""
    task_summary: str
    strategy: str
    steps: List[ConductorStep]
    run_id: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def layers_used(self) -> List[int]:
        return sorted(set(s.layer for s in self.steps))


@dataclass
class ConductorResult:
    """Final result of a conductor run."""
    run_id: str
    task: str
    plan: Optional[ConductorPlan]
    final_output: str
    steps_completed: int
    steps_failed: int
    total_duration_ms: int
    is_error: bool = False
    error: str = ""
