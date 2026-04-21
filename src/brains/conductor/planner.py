"""Conductor planner: system prompt, prompt builder, plan parser, simple plan."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import structlog

from .models import ConductorPlan, ConductorStep

logger = structlog.get_logger()


_PLANNER_SYSTEM = """\
You are AURA's task director. Your job is to analyze incoming tasks and \
create a precise multi-brain execution plan.

Available brains and their strengths:
- gemini      → web search, URL analysis, real-time data, research (Layer 1) [FREE - Google CLI]
- local-ollama → local code analysis, diagnosis, syntax review (Layer 1) [FREE - local]
- qwen-code   → code gen, multilingual, synthesis, analysis (Layer 1 & 2) [FREE - OAuth]
- openrouter  → text summarization, transformation, drafting (Layer 2) [FREE tier]
- opencode    → code refactoring, multi-file edits (Layer 2) [FREE tier]
- codex       → code execution, file writes, git commits (Layer 3) [ChatGPT Plus - PREFER FOR CODE]
- haiku       → text formatting, non-code answers, user-facing replies (Layer 3) [Claude Max - SAVE for text]
- sonnet      → complex reasoning, multi-file architecture (Layer 3, escalation) [Claude Max - use sparingly]
- opus        → deepest reasoning only (Layer 3, critical escalation) [Claude Max - rarely]

Layer philosophy:
  Layer 1: Gather / analyze / research (run first, free brains preferred)
  Layer 2: Synthesize / optimize / transform (feeds on Layer 1 output)
  Layer 3: Final execution / write output (PREFER codex for code, haiku for text only)

TOKEN CONSERVATION RULES (CRITICAL):
1. Use minimum brains needed — never over-engineer
2. Code tasks (fix, write, edit, commit) → ALWAYS use codex for L3 (not haiku)
3. Research/answer tasks → gemini (L1) + haiku (L3 final reply)
4. Simple code fix → codex directly (1 step, no L1 needed)
5. Analysis tasks → local-ollama (L1) + haiku or codex (L3)
6. Complex tasks → all 3 layers, codex for L3 if code involved
7. Each step prompt MUST be self-contained and specific
8. Reference earlier outputs as: {step_N_output}
9. If pending task IDs are provided below, reference them in task_id field

Return ONLY valid JSON, no markdown, no explanation:
{
  "task_summary": "one-line summary",
  "strategy": "why this approach",
  "steps": [
    {
      "step": 1,
      "layer": 1,
      "brain": "gemini",
      "role": "researcher",
      "prompt": "specific task for this brain",
      "depends_on": [],
      "task_id": "optional-uuid-if-linked-to-task-store"
    }
  ]
}
"""


def _build_planner_prompt(
    task: str,
    available_brains: List[str],
    pending_tasks: Optional[List[Dict[str, Any]]] = None,
    meta_context: str = "",
) -> str:
    """Build planner prompt with AURA self-knowledge + pending task context.

    meta_context is the ADENTRO layer — AURA's self-knowledge about what
    was tried, what failed, which brains are healthy. Injected into every
    planner call whether the task is internal (self-improvement) or external
    (Ricardo's request from Telegram).
    """
    available_str = ", ".join(available_brains)
    prompt_parts = [
        f"Available brains right now: {available_str}\n",
        f"Task to orchestrate:\n{task}\n",
    ]

    # ADENTRO — inject self-knowledge so planner avoids repeating mistakes
    if meta_context:
        prompt_parts.append(f"\n## AURA Self-Knowledge (use to inform your strategy):\n{meta_context}\n")

    # Include pending tasks if available
    if pending_tasks and len(pending_tasks) > 0:
        prompt_parts.append("\nPending tasks (you can reference these task IDs in your plan):")
        for t in pending_tasks[:10]:
            task_id = t.get("id", "")
            title = t.get("title", "")[:50]
            priority = t.get("priority", "medium")
            prompt_parts.append(f"  - [{task_id}] {title} (priority: {priority})")

    prompt_parts.append("\nReturn the JSON execution plan.")
    return "".join(prompt_parts)


def _parse_plan(raw: str, run_id: str) -> Optional[ConductorPlan]:
    """Extract and parse JSON plan from Claude's response."""
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("```").strip()

    # Find first { ... } block
    match = re.search(r"\{[\s\S]+\}", cleaned)
    if not match:
        logger.warning("conductor_no_json", raw=raw[:200])
        return None

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as e:
        logger.warning("conductor_json_error", error=str(e), raw=raw[:200])
        return None

    steps_raw = data.get("steps", [])
    if not steps_raw:
        return None

    steps = []
    for s in steps_raw:
        steps.append(ConductorStep(
            step=int(s.get("step", len(steps) + 1)),
            layer=int(s.get("layer", 3)),
            brain=str(s.get("brain", "haiku")),
            role=str(s.get("role", "executor")),
            prompt=str(s.get("prompt", "")),
            depends_on=[int(d) for d in s.get("depends_on", [])],
        ))

    return ConductorPlan(
        task_summary=str(data.get("task_summary", "Task"))[:120],
        strategy=str(data.get("strategy", ""))[:300],
        steps=steps,
        run_id=run_id,
    )


def _simple_plan(task: str, brain: str, run_id: str) -> ConductorPlan:
    """Fallback single-step plan when planner fails."""
    return ConductorPlan(
        task_summary=task[:80],
        strategy="Direct execution (planner unavailable)",
        steps=[ConductorStep(
            step=1, layer=3, brain=brain,
            role="executor", prompt=task,
        )],
        run_id=run_id,
    )
