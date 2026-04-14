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

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# ── Event bus (pub/sub for SSE clients) ──────────────────────────────────────

_subscribers: List[asyncio.Queue] = []


def orch_subscribe() -> asyncio.Queue:
    """Subscribe to orchestration events. Returns a queue to read from."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(q)
    return q


def orch_unsubscribe(q: asyncio.Queue) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


async def _broadcast(event: Dict[str, Any]) -> None:
    """Broadcast event to all SSE subscribers (non-blocking)."""
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)  # slow / disconnected client
    for q in dead:
        orch_unsubscribe(q)


# ── Data models ───────────────────────────────────────────────────────────────

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


# ── Planner prompt ────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are AURA's task director. Your job is to analyze incoming tasks and \
create a precise multi-brain execution plan.

Available brains and their strengths:
- gemini      → web search, URL analysis, real-time data, research (Layer 1)
- ollama-rud  → code analysis, local inference, unlimited free (Layer 1)
- qwen-code   → code gen, multilingual, synthesis, analysis (Layer 1 & 2)
- openrouter  → flexible LLM, summarization, transformation (Layer 2)
- opencode    → code generation, refactoring (Layer 2)
- haiku       → fast Claude, structured output, final polish (Layer 3)
- sonnet      → complex Claude, multi-step reasoning (Layer 3)
- opus        → deepest reasoning, architecture (Layer 3, escalation only)

Layer philosophy:
  Layer 1: Gather / analyze / research (run first, often in parallel)
  Layer 2: Synthesize / optimize / transform (feeds on Layer 1 output)
  Layer 3: Final execution / formatted output (feeds on all previous)

Rules:
1. Use minimum brains needed — don't over-engineer simple tasks
2. Simple tasks (1 brain): just Layer 3 directly
3. Research tasks: Layer 1 (gemini) → Layer 3 (haiku)
4. Code tasks: Layer 1 (ollama-rud/qwen-code) → Layer 3 (haiku/sonnet)
5. Complex tasks: all 3 layers
6. Each step's prompt MUST be self-contained and specific
7. Reference earlier outputs as: {step_N_output}

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
      "depends_on": []
    }
  ]
}
"""


def _build_planner_prompt(task: str, available_brains: List[str]) -> str:
    available_str = ", ".join(available_brains)
    return (
        f"Available brains right now: {available_str}\n\n"
        f"Task to orchestrate:\n{task}\n\n"
        "Return the JSON execution plan."
    )


# ── Plan parser ───────────────────────────────────────────────────────────────

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


# ── Conductor ─────────────────────────────────────────────────────────────────

class Conductor:
    """3-layer brain orchestrator.

    Usage:
        conductor = Conductor(brain_router)
        result = await conductor.run("Write a blog post about AI agents")
        print(result.final_output)
    """

    def __init__(
        self,
        brain_router: Any,
        notify_fn: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._router = brain_router
        self._notify = notify_fn  # Telegram callback for live updates

    async def _notify_safe(self, msg: str) -> None:
        if self._notify:
            try:
                result = self._notify(msg)
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    async def _create_plan(
        self,
        task: str,
        available_brains: List[str],
        run_id: str,
    ) -> ConductorPlan:
        """Ask Claude-haiku to create an execution plan."""
        haiku = self._router.get_brain("haiku")
        if not haiku:
            logger.warning("conductor_no_haiku_planner")
            return _simple_plan(task, "haiku", run_id)

        planner_prompt = _build_planner_prompt(task, available_brains)

        await _broadcast({
            "type": "planning",
            "run_id": run_id,
            "msg": "Claude analyzing task…",
            "ts": time.time(),
        })

        try:
            resp = await asyncio.wait_for(
                haiku.execute(
                    prompt=f"{_PLANNER_SYSTEM}\n\n{planner_prompt}",
                    timeout_seconds=30,
                ),
                timeout=35,
            )
            if resp.is_error or not resp.content:
                raise RuntimeError(resp.content or "empty planner response")

            plan = _parse_plan(resp.content, run_id)
            if plan:
                return plan

            logger.warning("conductor_plan_parse_failed", content=resp.content[:300])
        except Exception as e:
            logger.warning("conductor_planner_error", error=str(e))

        # Fallback: single-step plan
        return _simple_plan(task, "haiku", run_id)

    def _interpolate_prompt(
        self,
        prompt: str,
        step_outputs: Dict[int, str],
    ) -> str:
        """Replace {step_N_output} placeholders with actual outputs."""
        for step_num, output in step_outputs.items():
            prompt = prompt.replace(
                f"{{step_{step_num}_output}}",
                output[:2000],  # cap to avoid prompt explosion
            )
        return prompt

    async def _execute_step(
        self,
        step: ConductorStep,
        step_outputs: Dict[int, str],
        run_id: str,
    ) -> str:
        """Execute a single plan step against the assigned brain."""
        brain = self._router.get_brain(step.brain)

        # Fallback cascade if assigned brain not available
        if not brain:
            fallback_order = ["haiku", "qwen-code", "gemini", "sonnet"]
            for fb in fallback_order:
                brain = self._router.get_brain(fb)
                if brain:
                    step.brain = fb
                    break

        if not brain:
            step.status = "failed"
            step.error = "no brain available"
            await _broadcast({
                "type": "step_failed",
                "run_id": run_id,
                "step": step.step,
                "brain": step.brain,
                "error": "no brain available",
                "ts": time.time(),
            })
            return ""

        # Interpolate dependencies into prompt
        interpolated = self._interpolate_prompt(step.prompt, step_outputs)

        # Add context from dependency steps
        if step.depends_on:
            context_parts = []
            for dep in step.depends_on:
                if dep in step_outputs:
                    context_parts.append(
                        f"[Step {dep} output]\n{step_outputs[dep][:1500]}"
                    )
            if context_parts:
                interpolated = "\n\n".join(context_parts) + "\n\n" + interpolated

        step.status = "running"
        start = time.time()

        await _broadcast({
            "type": "step_started",
            "run_id": run_id,
            "step": step.step,
            "layer": step.layer,
            "brain": step.brain,
            "role": step.role,
            "prompt_preview": step.prompt[:100],
            "ts": start,
        })

        brain_emoji = getattr(brain, "emoji", "●")
        brain_display = getattr(brain, "display_name", step.brain)
        await self._notify_safe(
            f"{brain_emoji} <b>[{step.role.title()}]</b> {brain_display} working…"
        )

        try:
            resp = await asyncio.wait_for(
                brain.execute(prompt=interpolated),
                timeout=120,
            )
            elapsed = int((time.time() - start) * 1000)
            step.duration_ms = elapsed

            if resp.is_error:
                step.status = "failed"
                step.error = resp.error_type or "unknown"
                output = ""
                await _broadcast({
                    "type": "step_failed",
                    "run_id": run_id,
                    "step": step.step,
                    "brain": step.brain,
                    "error": step.error,
                    "duration_ms": elapsed,
                    "ts": time.time(),
                })
            else:
                step.status = "done"
                output = resp.content or ""
                await _broadcast({
                    "type": "step_completed",
                    "run_id": run_id,
                    "step": step.step,
                    "layer": step.layer,
                    "brain": step.brain,
                    "role": step.role,
                    "duration_ms": elapsed,
                    "output_preview": output[:200],
                    "ts": time.time(),
                })
                await self._notify_safe(
                    f"✅ <b>[{step.role.title()}]</b> {brain_display} done ({elapsed}ms)"
                )

            step.output = output
            return output

        except asyncio.TimeoutError:
            elapsed = int((time.time() - start) * 1000)
            step.status = "failed"
            step.error = "timeout"
            step.duration_ms = elapsed
            await _broadcast({
                "type": "step_failed",
                "run_id": run_id,
                "step": step.step,
                "brain": step.brain,
                "error": "timeout",
                "duration_ms": elapsed,
                "ts": time.time(),
            })
            return ""

        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            step.status = "failed"
            step.error = str(exc)
            step.duration_ms = elapsed
            await _broadcast({
                "type": "step_failed",
                "run_id": run_id,
                "step": step.step,
                "brain": step.brain,
                "error": str(exc)[:100],
                "duration_ms": elapsed,
                "ts": time.time(),
            })
            return ""

    async def run(
        self,
        task: str,
        run_id: Optional[str] = None,
        working_directory: str = "",
    ) -> ConductorResult:
        """Execute the full 3-layer orchestration run.

        Returns ConductorResult with final_output and full telemetry.
        """
        import uuid
        run_id = run_id or str(uuid.uuid4())[:8]
        start = time.time()

        logger.info("conductor_run_start", run_id=run_id, task=task[:80])

        # Available brains (skip internal ones)
        _PLANNABLE = [
            "api-zero", "ollama-rud", "qwen-code", "opencode",
            "gemini", "openrouter", "cline", "codex",
            "haiku", "sonnet", "opus",
        ]
        available = [
            b for b in _PLANNABLE
            if self._router.get_brain(b) is not None
        ]

        # Create plan
        plan = await self._create_plan(task, available, run_id)

        await _broadcast({
            "type": "plan_created",
            "run_id": run_id,
            "task": task[:120],
            "task_summary": plan.task_summary,
            "strategy": plan.strategy,
            "total_steps": plan.total_steps,
            "layers": plan.layers_used,
            "steps": [
                {
                    "step": s.step,
                    "layer": s.layer,
                    "brain": s.brain,
                    "role": s.role,
                    "depends_on": s.depends_on,
                }
                for s in plan.steps
            ],
            "ts": time.time(),
        })

        await self._notify_safe(
            f"🎯 <b>Plan ready</b> — {plan.total_steps} step(s) across "
            f"{len(plan.layers_used)} layer(s)\n"
            f"<i>{plan.strategy[:150]}</i>"
        )

        # Execute steps — group by layer, layers run sequentially, steps within
        # a layer that don't depend on each other run in parallel.
        step_outputs: Dict[int, str] = {}
        steps_completed = 0
        steps_failed = 0

        for layer_num in plan.layers_used:
            layer_steps = [s for s in plan.steps if s.layer == layer_num]

            # Separate steps with satisfied deps vs. blocked
            ready = [s for s in layer_steps if not s.depends_on or
                     all(d in step_outputs for d in s.depends_on)]
            blocked = [s for s in layer_steps if s not in ready]

            # Run ready steps in parallel within this layer
            if ready:
                tasks = [
                    self._execute_step(s, step_outputs, run_id)
                    for s in ready
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for step, result in zip(ready, results):
                    if isinstance(result, Exception):
                        step.status = "failed"
                        step.error = str(result)
                        steps_failed += 1
                    else:
                        output = result or ""
                        step_outputs[step.step] = output
                        if step.status == "done":
                            steps_completed += 1
                        else:
                            steps_failed += 1

            # Run any blocked steps sequentially after deps resolve
            for step in blocked:
                deps_ready = all(d in step_outputs for d in step.depends_on)
                if not deps_ready:
                    # Skip — dependency failed
                    step.status = "failed"
                    step.error = "dependency not met"
                    steps_failed += 1
                    continue
                output = await self._execute_step(step, step_outputs, run_id)
                step_outputs[step.step] = output
                if step.status == "done":
                    steps_completed += 1
                else:
                    steps_failed += 1

        # Final output = last completed step's output
        final_output = ""
        for step in reversed(plan.steps):
            if step.status == "done" and step.output:
                final_output = step.output
                break

        total_ms = int((time.time() - start) * 1000)

        await _broadcast({
            "type": "run_completed",
            "run_id": run_id,
            "task": task[:120],
            "steps_completed": steps_completed,
            "steps_failed": steps_failed,
            "total_duration_ms": total_ms,
            "output_preview": final_output[:300],
            "ts": time.time(),
        })

        logger.info(
            "conductor_run_done",
            run_id=run_id,
            steps_ok=steps_completed,
            steps_fail=steps_failed,
            duration_ms=total_ms,
        )

        return ConductorResult(
            run_id=run_id,
            task=task,
            plan=plan,
            final_output=final_output,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            total_duration_ms=total_ms,
            is_error=(steps_completed == 0),
        )


# ── Singleton ──────────────────────────────────────────────────────────────────

_conductor: Optional[Conductor] = None


def get_conductor(brain_router: Any = None, notify_fn: Any = None) -> Optional[Conductor]:
    """Return global conductor. Creates one if brain_router is supplied."""
    global _conductor
    if _conductor is None and brain_router is not None:
        _conductor = Conductor(brain_router, notify_fn=notify_fn)
        logger.info("conductor_initialized")
    return _conductor


def set_conductor(conductor: Conductor) -> None:
    global _conductor
    _conductor = conductor
