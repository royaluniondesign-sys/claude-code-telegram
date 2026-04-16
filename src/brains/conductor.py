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
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()


def log_session(
    activity: str,
    brain: str = "",
    step: int = 0,
    duration_ms: int = 0,
    status: str = "completed",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Log autonomous brain activity to persistent session log.

    Args:
        activity: Description of the activity (e.g., "conductor_run", "step_executed")
        brain: Brain name that executed the activity
        step: Step number (if applicable)
        duration_ms: Duration of the activity in milliseconds
        status: Status of the activity ("completed", "failed", "pending")
        details: Optional dict with additional context
    """
    try:
        log_dir = Path.home() / '.aura' / 'memory'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'session_log.txt'

        session_data = {
            'timestamp': datetime.now().isoformat(),
            'activity': activity,
            'brain': brain,
            'step': step,
            'duration_ms': duration_ms,
            'status': status,
            'details': details or {},
        }

        with open(log_file, 'a') as f:
            f.write(json.dumps(session_data) + '\n')
    except Exception as e:
        logger.error("session_log_write_failed", error=str(e))


# Import task_store to fetch pending tasks for plan context
try:
    from ..infra import task_store
except ImportError:
    task_store = None

# Import conductor_learnings to persist run insights
try:
    from ..infra.conductor_learnings import save_learnings
except ImportError:
    save_learnings = None


def _format_ts(ts: float) -> str:
    """Convert unix timestamp to ISO-8601."""
    from datetime import UTC, datetime
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


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
        self._last_notify_ts: float = 0.0  # rate-limit: max 1 msg / 30s

    async def _notify_safe(self, msg: str) -> None:
        """Send Telegram notification, throttled to 1 per 30s to avoid flood bans."""
        if not self._notify:
            return
        now = time.time()
        if now - self._last_notify_ts < 30:
            return  # skip — too soon
        self._last_notify_ts = now
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

        # Fetch pending tasks to provide context to the planner
        pending_tasks = None
        if task_store:
            try:
                pending_tasks = task_store.list_tasks(status="pending", limit=10)
            except Exception as e:
                logger.debug("conductor_pending_tasks_error", error=str(e))

        # Build ADENTRO meta-context: AURA's self-knowledge about history,
        # brain health, mission progress, and what NOT to repeat.
        # This feeds into every planner call — internal AND external tasks.
        meta_ctx = ""
        try:
            from ..infra.meta_context import build_compact_context
            meta_ctx = build_compact_context()
        except Exception as _mce:
            logger.debug("meta_context_unavailable", error=str(_mce))

        planner_prompt = _build_planner_prompt(task, available_brains, pending_tasks, meta_ctx)

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
        """Execute a single conductor step against its assigned brain.

        Handles prompt interpolation (previous step outputs), brain routing,
        timeout enforcement, retries (2×), and SSE event broadcasting.

        Returns the step output string. Sets step.status / step.output / step.error.
        """
        step.status = "running"
        start = time.time()

        # Log START of step for all brains
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f'START - Step {step.step}: {step.role} [{step.brain}]')

        # Inject previous step outputs into prompt placeholders
        prompt = self._interpolate_prompt(step.prompt, step_outputs)

        await _broadcast({
            "type": "step_started",
            "run_id": run_id,
            "step": step.step,
            "layer": step.layer,
            "brain": step.brain,
            "role": step.role,
            "ts": time.time(),
        })

        await self._notify_safe(
            f"🧠 <b>Step {step.step}</b> [{step.brain}] — {step.role}"
        )

        # Log autonomous brain activity (detailed session log)
        if step.brain == "autonomous":
            log_session(
                activity="step_started",
                brain=step.brain,
                step=step.step,
                status="running",
                details={"role": step.role, "prompt_length": len(prompt)},
            )

        brain = self._router.get_brain(step.brain)
        if not brain:
            step.status = "failed"
            step.error = f"brain '{step.brain}' not found in router"
            await _broadcast({
                "type": "step_failed",
                "run_id": run_id,
                "step": step.step,
                "brain": step.brain,
                "error": step.error,
                "duration_ms": int((time.time() - start) * 1000),
                "ts": time.time(),
            })
            return ""

        # Layer 3 (executor) gets longer timeout — writes files + commits
        timeout = 240 if step.layer >= 3 else 120

        output = ""
        last_error = ""
        for attempt in range(1, 3):  # up to 2 attempts
            try:
                resp = await asyncio.wait_for(
                    brain.execute(prompt, timeout_seconds=timeout),
                    timeout=timeout + 10,
                )
                if resp.is_error:
                    last_error = resp.content or "brain returned error"
                    logger.warning(
                        "conductor_step_brain_error",
                        run_id=run_id,
                        step=step.step,
                        attempt=attempt,
                        error=last_error[:120],
                    )
                    try:
                        from ..infra.rate_monitor import track_error
                        track_error(step.brain)
                    except Exception:
                        pass
                    if attempt < 2:
                        await asyncio.sleep(3)
                    continue
                # Track successful request in global rate monitor
                try:
                    from ..infra.rate_monitor import track_request
                    track_request(step.brain)
                except Exception:
                    pass
                output = resp.content or ""
                break
            except (asyncio.TimeoutError, Exception) as exc:
                last_error = str(exc)[:200]
                logger.warning(
                    "conductor_step_exception",
                    run_id=run_id,
                    step=step.step,
                    attempt=attempt,
                    error=last_error,
                )
                try:
                    from ..infra.rate_monitor import track_error
                    track_error(step.brain)
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(5)

        duration_ms = int((time.time() - start) * 1000)
        step.duration_ms = duration_ms

        if output:
            step.status = "done"
            step.output = output

            # Log autonomous brain success
            if step.brain == "autonomous":
                log_session(
                    activity="step_completed",
                    brain=step.brain,
                    step=step.step,
                    duration_ms=duration_ms,
                    status="completed",
                    details={"role": step.role, "output_length": len(output)},
                )

            await _broadcast({
                "type": "step_completed",
                "run_id": run_id,
                "step": step.step,
                "layer": step.layer,
                "brain": step.brain,
                "role": step.role,
                "output_preview": output[:300],
                "duration_ms": duration_ms,
                "ts": time.time(),
            })
        else:
            step.status = "failed"
            step.error = last_error or "no output after 2 attempts"

            # Log autonomous brain failure
            if step.brain == "autonomous":
                log_session(
                    activity="step_failed",
                    brain=step.brain,
                    step=step.step,
                    duration_ms=duration_ms,
                    status="failed",
                    details={"role": step.role, "error": step.error},
                )

            await _broadcast({
                "type": "step_failed",
                "run_id": run_id,
                "step": step.step,
                "brain": step.brain,
                "error": step.error,
                "duration_ms": duration_ms,
                "ts": time.time(),
            })

        # Log END of step for all brains
        logger.info(f'END - Step {step.step}: {step.status} ({duration_ms}ms)')

        logger.info(
            "conductor_step_done",
            run_id=run_id,
            step=step.step,
            brain=step.brain,
            status=step.status,
            duration_ms=duration_ms,
        )
        return output

    async def run_plan(
        self,
        plan: "ConductorPlan",
        task: str = "",
        run_id: Optional[str] = None,
        source: str = "manual",
    ) -> "ConductorResult":
        """Execute a pre-built plan directly, skipping the LLM planner.

        Use this when you need deterministic execution (e.g., proactive loop
        with a specific task from task_store).

        Args:
            plan: The execution plan to run
            task: Optional task description (defaults to plan.task_summary)
            run_id: Optional run identifier (auto-generated if not provided)
            source: Origin of the run — "manual", "proactive", or "scheduler"
        """
        import uuid
        run_id = run_id or str(uuid.uuid4())[:8]
        plan.run_id = run_id
        task = task or plan.task_summary
        start = time.time()

        # Set source for history tracking
        self._run_source = source

        logger.info("conductor_run_plan", run_id=run_id, task=task[:80], steps=plan.total_steps, source=source)

        await _broadcast({
            "type": "plan_created",
            "run_id": run_id,
            "task": task[:120],
            "task_summary": plan.task_summary,
            "strategy": plan.strategy,
            "total_steps": plan.total_steps,
            "layers": plan.layers_used,
            "steps": [
                {"step": s.step, "layer": s.layer, "brain": s.brain,
                 "role": s.role, "depends_on": s.depends_on}
                for s in plan.steps
            ],
            "ts": time.time(),
        })

        step_outputs: dict = {}
        steps_completed = 0
        steps_failed = 0

        for layer_num in plan.layers_used:
            layer_steps = [s for s in plan.steps if s.layer == layer_num]
            ready = [s for s in layer_steps if not s.depends_on or
                     all(d in step_outputs for d in s.depends_on)]
            blocked = [s for s in layer_steps if s not in ready]

            if ready:
                results = await asyncio.gather(
                    *[self._execute_step(s, step_outputs, run_id) for s in ready],
                    return_exceptions=True,
                )
                for step, result in zip(ready, results):
                    if isinstance(result, Exception):
                        step.status = "failed"
                        step.error = str(result)
                        steps_failed += 1
                    else:
                        step_outputs[step.step] = result or ""
                        if step.status == "done":
                            steps_completed += 1
                        else:
                            steps_failed += 1

            for step in blocked:
                if not all(d in step_outputs for d in step.depends_on):
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

        final_output = ""
        for step in reversed(plan.steps):
            if step.status == "done" and step.output:
                final_output = step.output
                break

        total_ms = int((time.time() - start) * 1000)

        # Log run completion if any autonomous brains were involved
        has_autonomous = any(s.brain == "autonomous" for s in plan.steps)
        if has_autonomous:
            log_session(
                activity="conductor_run_plan_completed",
                brain="autonomous",
                duration_ms=total_ms,
                status="completed" if steps_completed > 0 and steps_failed == 0 else "partial",
                details={
                    "run_id": run_id,
                    "task_summary": task[:120],
                    "steps_completed": steps_completed,
                    "steps_failed": steps_failed,
                    "source": source,
                },
            )

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

        logger.info("conductor_run_plan_done", run_id=run_id,
                    steps_ok=steps_completed, steps_fail=steps_failed, duration_ms=total_ms)

        result = ConductorResult(
            run_id=run_id, task=task, plan=plan,
            final_output=final_output,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            total_duration_ms=total_ms,
            is_error=(steps_completed == 0),
        )
        try:
            from ..infra.conductor_history import save_run
            save_run({
                "run_id": run_id,
                "task": task[:300],
                "task_summary": plan.task_summary,
                "strategy": plan.strategy,
                "source": getattr(self, "_run_source", "proactive"),
                "started_at": _format_ts(start),
                "completed_at": _format_ts(time.time()),
                "total_duration_ms": total_ms,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "is_error": result.is_error,
                "final_output": final_output[:600],
                "steps": [
                    {"step": s.step, "layer": s.layer, "brain": s.brain,
                     "role": s.role, "status": s.status,
                     "prompt": s.prompt if s.prompt else "",
                     "output": s.output[:400] if s.output else "",
                     "duration_ms": s.duration_ms, "error": s.error}
                    for s in plan.steps
                ],
            })
        except Exception:
            pass

        # Save learnings from this run
        if save_learnings:
            try:
                save_learnings(result)
            except Exception:
                pass

        return result

    async def run(
        self,
        task: str,
        run_id: Optional[str] = None,
        working_directory: str = "",
        source: str = "manual",
    ) -> ConductorResult:
        """Execute the full 3-layer orchestration run.

        Args:
            task: The task to orchestrate
            run_id: Optional run identifier (auto-generated if not provided)
            working_directory: Optional working directory context
            source: Origin of the run — "manual", "proactive", or "scheduler"

        Returns ConductorResult with final_output and full telemetry.
        """
        import uuid
        run_id = run_id or str(uuid.uuid4())[:8]
        start = time.time()

        # Set source for history tracking
        self._run_source = source

        logger.info("conductor_run_start", run_id=run_id, task=task[:80], source=source)

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

        # Log run completion if any autonomous brains were involved
        has_autonomous = any(s.brain == "autonomous" for s in plan.steps)
        if has_autonomous:
            log_session(
                activity="conductor_run_completed",
                brain="autonomous",
                duration_ms=total_ms,
                status="completed" if steps_completed > 0 and steps_failed == 0 else "partial",
                details={
                    "run_id": run_id,
                    "task_summary": task[:120],
                    "steps_completed": steps_completed,
                    "steps_failed": steps_failed,
                    "source": source,
                },
            )

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

        result = ConductorResult(
            run_id=run_id,
            task=task,
            plan=plan,
            final_output=final_output,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            total_duration_ms=total_ms,
            is_error=(steps_completed == 0),
        )

        # Save learnings (same as run_plan — keep both paths in sync)
        if save_learnings:
            try:
                save_learnings(result)
            except Exception:
                pass

        # Persist run to history (dashboard Sessions panel)
        try:
            from ..infra.conductor_history import save_run
            save_run({
                "run_id": run_id,
                "task": task[:300],
                "task_summary": plan.task_summary,
                "strategy": plan.strategy,
                "source": getattr(self, "_run_source", "manual"),
                "started_at": _format_ts(start),
                "completed_at": _format_ts(time.time()),
                "total_duration_ms": total_ms,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "is_error": result.is_error,
                "final_output": final_output[:600],
                "steps": [
                    {
                        "step": s.step,
                        "layer": s.layer,
                        "brain": s.brain,
                        "role": s.role,
                        "status": s.status,
                        "prompt": s.prompt if s.prompt else "",
                        "output": s.output[:400] if s.output else "",
                        "duration_ms": s.duration_ms,
                        "error": s.error,
                    }
                    for s in plan.steps
                ],
            })
        except Exception as _he:
            logger.debug("conductor_history_save_failed", error=str(_he))

        return result


# ── Metrics Visualization ─────────────────────────────────────────────────────

def visualize_metrics(plan: Optional[ConductorPlan] = None) -> None:
    """Visualize conductor metrics with consistent error handling.

    Args:
        plan: Optional conductor plan to visualize metrics for.

    Returns:
        None (logs errors consistently).
    """
    try:
        if plan is None:
            logger.warning("visualize_metrics_no_plan")
            return

        logger.debug("visualize_metrics_started", total_steps=plan.total_steps)

        # Validate plan structure
        if not plan.steps:
            logger.warning("visualize_metrics_empty_steps")
            raise ValueError("Plan has no steps to visualize")

        # Process metrics
        for step in plan.steps:
            if step.status not in ("pending", "running", "done", "failed"):
                logger.warning(
                    "visualize_metrics_invalid_step_status",
                    step=step.step,
                    status=step.status,
                )
                continue

        logger.info(
            "visualize_metrics_completed",
            total_steps=plan.total_steps,
            layers=len(plan.layers_used),
        )

    except ValueError as e:
        logger.error("visualize_metrics_validation_error", error=str(e))
    except AttributeError as e:
        logger.error("visualize_metrics_attribute_error", error=str(e))
    except Exception as e:
        logger.error(
            "visualize_metrics_unexpected_error",
            error=str(e),
            error_type=type(e).__name__,
        )


# ── Strategic Task Generation ─────────────────────────────────────────────────

def generate_tasks() -> List[Dict[str, Any]]:
    """Generate strategic tasks from mission goals with priority sorting.

    Reads mission goals from MISSION.md, parses criticality and impact,
    and returns prioritized task list sorted by criticality and impact.

    Returns:
        List of task dicts with 'description', 'criticality', 'impact' keys.
    """
    tasks = []
    mission_file_path = '/Users/oxyzen/claude-code-telegram/src/mission/MISSION.md'
    mission_goals = read_mission_file(mission_file_path)

    for goal in mission_goals:
        criticality = goal.get('criticality')
        impact = goal.get('impact')
        task = create_task(goal['description'], criticality, impact)
        tasks.append(task)

    # Prioritize tasks based on criticality and impact
    tasks.sort(key=lambda x: (x['criticality'], x['impact']), reverse=True)

    return tasks


def read_mission_file(file_path: str) -> List[Dict[str, Any]]:
    """Read and parse mission goals from file.

    Args:
        file_path: Path to MISSION.md file.

    Returns:
        List of goal dicts with 'criticality', 'impact', 'description' keys.
    """
    with open(file_path, 'r') as file:
        lines = file.readlines()
        mission_goals = []
        for line in lines:
            if line.strip():  # Skip empty lines
                goal = parse_line(line)
                mission_goals.append(goal)
        return mission_goals


def parse_line(line: str) -> Dict[str, Any]:
    """Parse a mission goal line into structured data.

    Expected format: "criticality: N, impact: N, description: text"

    Args:
        line: Raw mission goal line.

    Returns:
        Dict with 'criticality', 'impact', 'description' keys.
    """
    parts = line.split(', ')
    criticality = int(parts[0].split(': ')[1])
    impact = int(parts[1].split(': ')[1])
    description = parts[2].split(': ')[1]
    return {'criticality': criticality, 'impact': impact, 'description': description}


def create_task(description: str, criticality: int, impact: int) -> Dict[str, Any]:
    """Create a task dict from components.

    Args:
        description: Task description.
        criticality: Criticality level (int).
        impact: Impact level (int).

    Returns:
        Task dict with description, criticality, impact.
    """
    return {'description': description, 'criticality': criticality, 'impact': impact}


def log_conductor_run(tasks_executed: List[str], outcomes: List[str]) -> None:
    """Log conductor run learning to persistent memory.

    Args:
        tasks_executed: List of task descriptions/identifiers
        outcomes: List of outcomes ("success" or "failure" for each task)
    """
    log_path = Path.home() / '.aura' / 'memory' / 'conductor_log.md'

    # Ensure the log directory exists
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Get current date and time
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Log the run
    try:
        with open(log_path, 'a') as log_file:
            log_file.write(f"Date: {timestamp}\n")
            log_file.write(f"Tasks Executed: {tasks_executed}\n")
            log_file.write(f"Outcomes: {outcomes}\n\n")
    except Exception as e:
        logger.error("conductor_log_write_failed", error=str(e))


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
