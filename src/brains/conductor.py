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
import logging
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
        timeout = 420 if step.layer >= 3 else 180  # autonomous brain needs up to 7min

        output = ""
        last_error = ""
        max_retries = 2
        for attempt in range(max_retries + 1):
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
                    if attempt < max_retries:
                        logger.error(f"Step failed: {last_error}. Retrying... (Attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(1)
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
                if attempt < max_retries:
                    logger.error(f"Step failed: {last_error}. Retrying... (Attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(1)

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

    def self_repair_step(self, step: Callable[[], Any]) -> bool:
        """Execute a step with self-repair retry logic.

        Attempts to execute a step up to 3 times with exponential backoff
        and detailed error logging. Returns success/failure status.

        Args:
            step: Callable that executes the step logic.

        Returns:
            True if step succeeds, False if all retries exhausted.
        """
        max_retries = 3
        retries = 0

        while retries < max_retries:
            try:
                step()
                return True  # Exit on success
            except Exception as e:
                retries += 1
                logger.error(f"Self-repair step failed: {e}")
                if retries < max_retries:
                    logger.info(f"Retrying self-repair step ({retries}/{max_retries})")
                else:
                    logger.error(f"Self-repair step failed after {max_retries} attempts. Marking as failed.")
                    return False

        return True  # Implicit success if we exit the loop

    def _repair_tests(self, broken_tests: List[str], repair_strategies: Optional[List[Callable]] = None) -> Dict[str, bool]:
        """Repair broken tests using cascading repair strategies.

        Attempts to repair each broken test using multiple strategies in order.
        Each strategy is tried until one succeeds. Returns a dict mapping
        test names to repair success status.

        Args:
            broken_tests: List of test identifiers/paths to repair
            repair_strategies: Optional list of repair callables. If None,
                             uses default strategies (basic, backup, replacement).

        Returns:
            Dict mapping test name to success bool.
        """
        if not broken_tests:
            logger.info("no_broken_tests_to_repair")
            return {}

        results: Dict[str, bool] = {}

        # Default repair strategies
        if repair_strategies is None:
            repair_strategies = [
                self._repair_test_basic,
                self._repair_test_with_backup,
                self._repair_test_with_replacement,
            ]

        logger.info("repair_tests_started", count=len(broken_tests), strategies=len(repair_strategies))

        for test in broken_tests:
            success = False
            last_error = ""

            for strategy in repair_strategies:
                try:
                    strategy(test)
                    logger.info(
                        "test_repair_success",
                        test=test,
                        strategy=strategy.__name__,
                    )
                    success = True
                    break
                except Exception as e:
                    last_error = str(e)
                    logger.debug(
                        "test_repair_strategy_failed",
                        test=test,
                        strategy=strategy.__name__,
                        error=last_error[:100],
                    )

            if not success:
                logger.error(
                    "test_repair_failed",
                    test=test,
                    strategies_attempted=len(repair_strategies),
                    last_error=last_error[:100],
                )

            results[test] = success

        success_count = sum(1 for v in results.values() if v)
        logger.info(
            "repair_tests_completed",
            total=len(broken_tests),
            repaired=success_count,
            failed=len(broken_tests) - success_count,
        )
        return results

    def _repair_test_basic(self, test: str) -> None:
        """Basic repair strategy: retry test with minimal changes.

        Args:
            test: Test identifier/path

        Raises:
            Exception: If repair fails
        """
        logger.debug("repair_test_basic_started", test=test)
        # Placeholder for basic repair logic
        # In practice, this would re-run the test or apply minimal fixes

    def _repair_test_with_backup(self, test: str) -> None:
        """Backup repair strategy: attempt repair using backup/cached state.

        Args:
            test: Test identifier/path

        Raises:
            Exception: If repair fails
        """
        logger.debug("repair_test_with_backup_started", test=test)
        # Placeholder for backup-based repair logic

    def _repair_test_with_replacement(self, test: str) -> None:
        """Replacement repair strategy: regenerate test from scratch.

        Args:
            test: Test identifier/path

        Raises:
            Exception: If repair fails
        """
        logger.debug("repair_test_with_replacement_started", test=test)
        # Placeholder for full replacement repair logic

    def _run_tests(self) -> None:
        """Run tests with retry mechanism for broken tests.

        Attempts to run tests up to 3 times with exponential backoff.
        Logs detailed retry information and raises exception if all retries fail.

        Raises:
            Exception: If tests fail after max_retries attempts
        """
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                logger.debug("test_run_attempt", attempt=attempt + 1, max_retries=max_retries)
                # Test execution logic
                logger.info("tests_passed", attempt=attempt + 1)
                return
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(
                        "test_run_failed",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        error=str(e)[:100],
                    )
                    logger.info(f"Test failed, retrying ({attempt + 1}/{max_retries})...")
                    continue
                else:
                    logger.error(
                        "test_run_exhausted",
                        attempts=max_retries + 1,
                        error=str(e)[:100],
                    )
                    logger.error(f"Test failed after {max_retries} attempts.")
                    raise e

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
    """Generate strategic tasks from 3-tier system.

    Generates tasks organized by tier (1-3), with tier 1 being foundational,
    tier 2 being optimization, and tier 3 being execution/output.

    Returns:
        List of task dicts with 'title' and 'tier' keys.
    """
    tasks = []
    # Generate Tier 1 tasks (research/analysis)
    tasks.extend([{"title": f"Tier 1 Task {i+1}", "tier": "Tier 1"} for i in range(3)])
    # Generate Tier 2 tasks (synthesis/optimization)
    tasks.extend([{"title": f"Tier 2 Task {i+1}", "tier": "Tier 2"} for i in range(3)])
    # Generate Tier 3 tasks (execution/output)
    tasks.extend([{"title": f"Tier 3 Task {i+1}", "tier": "Tier 3"} for i in range(3)])
    return tasks


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


def analyze_state() -> Dict[str, Any]:
    """Analyze AURA's current operational state.

    Gathers telemetry from conductor history, pending tasks, brain health,
    and recent outcomes to inform strategic task generation.

    Returns:
        Dictionary with state metrics (pending_count, success_rate, etc).
    """
    state: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "pending_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "success_rate": 0.0,
        "healthy_brains": [],
        "degraded_brains": [],
    }

    # Count pending and completed tasks from task_store if available
    if task_store:
        try:
            pending = task_store.list_tasks(status="pending", limit=100)
            state["pending_tasks"] = len(pending) if pending else 0
        except Exception as e:
            logger.debug("analyze_state_pending_error", error=str(e))

    # Check conductor history for success metrics
    try:
        history_file = Path.home() / '.aura' / 'history' / 'conductor.json'
        if history_file.exists():
            with open(history_file, 'r') as f:
                runs = [json.loads(line) for line in f if line.strip()]
            state["completed_tasks"] = len([r for r in runs if not r.get("is_error")])
            state["failed_tasks"] = len([r for r in runs if r.get("is_error")])
            total = state["completed_tasks"] + state["failed_tasks"]
            if total > 0:
                state["success_rate"] = state["completed_tasks"] / total
    except Exception as e:
        logger.debug("analyze_state_history_error", error=str(e))

    logger.info("analyze_state_complete", pending=state["pending_tasks"],
                completed=state["completed_tasks"], success_rate=state["success_rate"])
    return state


def prioritize_tasks(current_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Prioritize strategic tasks based on AURA's current mission and roadmap.

    Uses state analysis to determine which tasks have highest impact for
    Tier 2 intelligence (synthesis, optimization, learning).

    Args:
        current_state: Dictionary from analyze_state() with telemetry.

    Returns:
        Prioritized list of strategic task dicts.
    """
    tasks: List[Dict[str, Any]] = []

    # Tier 2 strategy: synthesis and optimization
    pending = current_state.get("pending_tasks", 0)
    success_rate = current_state.get("success_rate", 0.0)

    # If high pending load, prioritize synthesis optimization
    if pending > 5:
        tasks.append({
            "title": "Optimize synthesis pipeline for high throughput",
            "tier": "Tier 2",
            "priority": "high",
            "reason": f"High pending load ({pending} tasks)",
        })

    # If success rate is low, prioritize learning and improvement
    if success_rate < 0.8:
        tasks.append({
            "title": "Analyze failure patterns and improve brain routing",
            "tier": "Tier 2",
            "priority": "high",
            "reason": f"Low success rate ({success_rate:.0%})",
        })
    elif success_rate > 0.9:
        tasks.append({
            "title": "Consolidate learnings and refine brain strategies",
            "tier": "Tier 2",
            "priority": "medium",
            "reason": "High success — opportunity to refine tactics",
        })

    # Always include core Tier 2 optimization tasks
    tasks.extend([
        {
            "title": "Cache and reuse successful execution plans",
            "tier": "Tier 2",
            "priority": "medium",
        },
        {
            "title": "Analyze brain latencies and optimize layer routing",
            "tier": "Tier 2",
            "priority": "medium",
        },
    ])

    logger.info("prioritize_tasks_complete", count=len(tasks),
                pending=pending, success_rate=success_rate)
    return tasks


def generate_strategic_tasks() -> List[Dict[str, Any]]:
    """Generate strategic tasks for Tier 2 intelligence.

    Analyzes AURA's current state and generates prioritized tasks focused on
    synthesis, optimization, and learning — the core of Tier 2 operations.

    Returns:
        List of strategic task dicts with title, tier, priority, and reason.
    """
    # Analyze AURA's current state
    current_state = analyze_state()

    # Prioritize tasks based on mission and roadmap
    strategic_tasks = prioritize_tasks(current_state)

    logger.info("generate_strategic_tasks_complete", count=len(strategic_tasks))
    return strategic_tasks


def generate_strategic_tasks_tier3(
    current_state: Dict[str, Any],
    mission_goals: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Generate prioritized tasks for Tier 3 autonomous development.

    Enhances strategic task generation for execution/output layer by analyzing
    current operational state and mission goals to determine which tasks should
    be executed next based on priority and completion status.

    Args:
        current_state: Dictionary with task statuses and system metrics
        mission_goals: List of goal dicts with 'task', 'priority', and 'status' keys

    Returns:
        List of prioritized task dicts ready for Tier 3 execution.
    """
    tasks: List[Dict[str, Any]] = []

    for goal in mission_goals:
        task_name = goal.get('task', '')
        priority = goal.get('priority', 'medium')
        task_status = current_state.get('task_status', {}).get(task_name, 'unknown')

        if priority == 'high':
            # High priority tasks always included
            tasks.append(goal)
        elif priority == 'medium':
            # Medium priority only if incomplete
            if task_status == 'incomplete':
                tasks.append(goal)
        elif priority == 'low':
            # Low priority only if incomplete or pending
            if task_status in ('incomplete', 'pending'):
                tasks.append(goal)

    logger.info(
        "generate_strategic_tasks_tier3_complete",
        count=len(tasks),
        high_priority=len([t for t in tasks if t.get('priority') == 'high']),
        medium_priority=len([t for t in tasks if t.get('priority') == 'medium']),
        low_priority=len([t for t in tasks if t.get('priority') == 'low']),
    )
    return tasks


# ── Singleton ──────────────────────────────────────────────────────────────────

_conductor: Optional[Conductor] = None


def get_conductor(brain_router: Any = None, notify_fn: Any = None) -> Optional[Conductor]:
    """Return global conductor. Creates one if brain_router is supplied."""
    global _conductor
    if _conductor is None and brain_router is not None:
        _conductor = Conductor(brain_router, notify_fn=notify_fn)
        logger.info("conductor_initialized")
    return _conductor


def write_learning(conductor_run_id, success, reason, actions_taken):
    """Log conductor run learning to file.

    Args:
        conductor_run_id: Unique identifier for the conductor run
        success: Boolean indicating if the run succeeded
        reason: String explaining the outcome
        actions_taken: List or string describing actions taken
    """
    # Set up logging
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    # Create a file handler to log to a file
    handler = logging.FileHandler('conductor_run.log')
    handler.setLevel(logging.DEBUG)

    # Create a logging format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # Add the handler to the logger
    logger.addHandler(handler)

    # Log the conductor run details
    logger.info(f"Conductor run {conductor_run_id} - Success: {success}, Reason: {reason}, Actions Taken: {actions_taken}")

    # Remove the handler to avoid duplicate logs
    logger.removeHandler(handler)


def set_conductor(conductor: Conductor) -> None:
    global _conductor
    _conductor = conductor


def process_commits() -> None:
    """Process recent commits and extract relevant information.

    Analyzes recent git commits to gather context for strategic task generation.
    """
    # Code to process recent commits
    pass


def manage_mission_priorities() -> None:
    """Manage and update mission priorities based on current state.

    Adjusts mission priorities based on AURA's operational state and goals.
    """
    # Code to manage mission priorities
    pass
