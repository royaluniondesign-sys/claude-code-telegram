"""The Conductor class — 3-layer brain orchestrator."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

import structlog

from .events import _broadcast
from .logging_utils import _format_ts, log_session
from .models import ConductorPlan, ConductorResult, ConductorStep
from .planner import _PLANNER_SYSTEM, _build_planner_prompt, _parse_plan, _simple_plan
from .step_executor import execute_step

logger = structlog.get_logger()

# Import task_store to fetch pending tasks for plan context
try:
    from ...infra import task_store
except ImportError:
    task_store = None

# Import conductor_learnings to persist run insights
try:
    from ...infra.conductor_learnings import save_learnings
except ImportError:
    save_learnings = None


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
        self._run_source: str = "manual"

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
        meta_ctx = ""
        try:
            from ...infra.meta_context import build_compact_context
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

    # ── Thin wrappers for extracted module-level functions ────────────────────

    def _interpolate_prompt(self, prompt: str, step_outputs: Dict[int, str]) -> str:
        from .step_executor import interpolate_prompt
        return interpolate_prompt(prompt, step_outputs)

    async def _execute_step(
        self,
        step: ConductorStep,
        step_outputs: Dict[int, str],
        run_id: str,
    ) -> str:
        return await execute_step(
            step=step,
            step_outputs=step_outputs,
            run_id=run_id,
            router=self._router,
            run_source=self._run_source,
        )

    def self_repair_step(self, step: Any) -> bool:
        from .repair import self_repair_step as _self_repair_step
        return _self_repair_step(step)

    def _repair_tests(self, broken_tests: Any, repair_strategies: Any = None) -> Any:
        from .repair import _repair_tests as _rt
        return _rt(broken_tests, repair_strategies)

    def _repair_test_basic(self, test: str) -> None:
        from .repair import _repair_test_basic
        _repair_test_basic(test)

    def _repair_test_with_backup(self, test: str) -> None:
        from .repair import _repair_test_with_backup
        _repair_test_with_backup(test)

    def _repair_test_with_replacement(self, test: str) -> None:
        from .repair import _repair_test_with_replacement
        _repair_test_with_replacement(test)

    def retry_broken_tests(self, test: str, result: Any) -> bool:
        from .repair import retry_broken_tests
        return retry_broken_tests(test, result)

    def _run_tests(self) -> None:
        from .repair import _run_tests
        _run_tests()

    def self_repair(self) -> None:
        from .repair import self_repair
        self_repair()

    def self_repair_launch_agent(self) -> None:
        from .repair import self_repair_launch_agent
        self_repair_launch_agent()

    # ── Core orchestration: run_plan ──────────────────────────────────────────

    async def run_plan(
        self,
        plan: ConductorPlan,
        task: str = "",
        run_id: Optional[str] = None,
        source: str = "manual",
    ) -> ConductorResult:
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

        try:
            return await self._execute_run_plan(plan, task, run_id, source, start)
        except asyncio.CancelledError:
            logger.info("conductor_run_plan_cancelled", run_id=run_id)
            return ConductorResult(
                run_id=run_id, task=task, plan=plan,
                final_output="",
                steps_completed=0, steps_failed=0,
                total_duration_ms=int((time.time() - start) * 1000),
                is_error=True,
            )

    async def _execute_run_plan(
        self,
        plan: ConductorPlan,
        task: str,
        run_id: str,
        source: str,
        start: float,
    ) -> ConductorResult:
        """Inner execution of run_plan logic."""
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

        step_outputs: Dict[int, str] = {}
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
            from ...infra.conductor_history import save_run
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

    # ── Core orchestration: run ───────────────────────────────────────────────

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

        try:
            return await self._execute_run(task, run_id, working_directory, source, start)
        except asyncio.CancelledError:
            logger.info("conductor_run_cancelled", run_id=run_id)
            return ConductorResult(
                run_id=run_id, task=task, plan=None,
                final_output="",
                steps_completed=0, steps_failed=0,
                total_duration_ms=int((time.time() - start) * 1000),
                is_error=True,
            )

    async def _execute_run(
        self,
        task: str,
        run_id: str,
        working_directory: str,
        source: str,
        start: float,
    ) -> ConductorResult:
        """Inner execution of run logic."""

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
            from ...infra.conductor_history import save_run
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
