"""AURA Conductor — 3-layer orchestration engine.

Claude is the DIRECTOR, not the executor.

Flow:
  1. Claude-haiku analyzes the task → returns a JSON execution plan
  2. Conductor parses the plan (layers, brain assignments, step prompts)
  3. Each step runs against the real Brain implementations
  4. Context from completed steps feeds forward into subsequent steps
  5. Events broadcast in real-time → dashboard SSE stream

Three-layer philosophy:
  Layer 1 — Analysis / Research   (api-zero, ollama-rud, qwen-code, gemini)
  Layer 2 — Synthesis / Optimize  (qwen-code, opencode, openrouter, gemini)
  Layer 3 — Execution / Output    (codex, haiku, sonnet, opus)

Claude's role: understand the task, decide which brains are needed,
assign precise sub-prompts to each, and let specialists execute.

Event types (broadcast via orch_bus):
  plan_created       — plan JSON ready, steps count known
  step_started       — a brain just received its task
  step_completed     — brain finished, content available
  step_failed        — brain errored, conductor cascading
  run_completed      — all steps done, final output ready
  run_failed         — fatal error, no output
"""
from __future__ import annotations

# Data models
from .models import ConductorResult, ConductorPlan, ConductorStep

# Event bus — re-export the actual list object so
# `from src.brains.conductor import _subscribers` still works
from .events import _subscribers, orch_subscribe, orch_unsubscribe, _broadcast

# Logging utilities
from .logging_utils import log_session, log_conductor_run, write_learning, _format_ts

# Planner
from .planner import (
    _PLANNER_SYSTEM,
    _build_planner_prompt,
    _parse_plan,
    _simple_plan,
)

# Step executor
from .step_executor import interpolate_prompt, execute_step

# Repair
from .repair import (
    self_repair_step,
    _repair_tests,
    _repair_test_basic,
    _repair_test_with_backup,
    _repair_test_with_replacement,
    retry_broken_tests,
    _run_tests,
    self_repair,
    self_repair_launch_agent,
)

# Strategic tasks
from .strategic_tasks import (
    visualize_metrics,
    generate_tasks,
    analyze_state,
    prioritize_tasks,
    generate_strategic_tasks,
    generate_strategic_tasks_tier3,
    process_commits,
    manage_mission_priorities,
)

# Singleton
from .singleton import get_conductor, set_conductor

# Conductor class
from .orchestrator import Conductor

__all__ = [
    # Models
    "ConductorResult",
    "ConductorPlan",
    "ConductorStep",
    # Events
    "_subscribers",
    "orch_subscribe",
    "orch_unsubscribe",
    "_broadcast",
    # Logging
    "log_session",
    "log_conductor_run",
    "write_learning",
    "_format_ts",
    # Planner
    "_PLANNER_SYSTEM",
    "_build_planner_prompt",
    "_parse_plan",
    "_simple_plan",
    # Step executor
    "interpolate_prompt",
    "execute_step",
    # Repair
    "self_repair_step",
    "_repair_tests",
    "_repair_test_basic",
    "_repair_test_with_backup",
    "_repair_test_with_replacement",
    "retry_broken_tests",
    "_run_tests",
    "self_repair",
    "self_repair_launch_agent",
    # Strategic tasks
    "visualize_metrics",
    "generate_tasks",
    "analyze_state",
    "prioritize_tasks",
    "generate_strategic_tasks",
    "generate_strategic_tasks_tier3",
    "process_commits",
    "manage_mission_priorities",
    # Singleton
    "get_conductor",
    "set_conductor",
    # Orchestrator
    "Conductor",
]
