"""Step execution logic: prompt interpolation and execute_step."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict

import structlog

from .events import _broadcast, orch_unsubscribe
from .logging_utils import log_session
from .models import ConductorStep

logger = structlog.get_logger()


def interpolate_prompt(
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


async def execute_step(
    step: ConductorStep,
    step_outputs: Dict[int, str],
    run_id: str,
    router: Any,
    run_source: str = "manual",
) -> str:
    """Execute a single conductor step against its assigned brain.

    Handles prompt interpolation (previous step outputs), brain routing,
    timeout enforcement, and retry logic (2 retries max).
    Broadcasts SSE events for step lifecycle (started, completed, failed).

    Returns the step output string. Sets step.status / step.output / step.error.
    """
    step.status = "running"
    start = time.time()

    # Log START of step for all brains
    logger.info(f'START - Step {step.step}: {step.role} [{step.brain}]')

    # Inject previous step outputs into prompt placeholders
    prompt = interpolate_prompt(step.prompt, step_outputs)

    # ── Safety: proactive/scheduler runs MUST NOT commit code ──────────────
    if run_source in ("proactive", "scheduler") and step.layer >= 3:
        prompt = (
            "IMPORTANT CONSTRAINT: This is an autonomous proactive task.\n"
            "DO NOT run git commit, git add, git push, or modify any source code files.\n"
            "DO NOT write to src/ files. Only read files, analyze, and produce a text report.\n"
            "If you find something that needs fixing, DESCRIBE the fix but do not implement it.\n\n"
        ) + prompt

    await _broadcast({
        "type": "step_started",
        "run_id": run_id,
        "step": step.step,
        "layer": step.layer,
        "brain": step.brain,
        "role": step.role,
        "ts": time.time(),
    })

    # Log autonomous brain activity (detailed session log)
    if step.brain == "autonomous":
        log_session(
            activity="step_started",
            brain=step.brain,
            step=step.step,
            status="running",
            details={"role": step.role, "prompt_length": len(prompt)},
        )

    brain = router.get_brain(step.brain)
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
    timeout = 180  # max 3min per step — prevents handler hangs

    output = ""
    last_error = ""
    max_retries = 2
    retries = 0

    while retries <= max_retries:
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
                    attempt=retries,
                    error=last_error[:120],
                )
                try:
                    from ...infra.rate_monitor import track_error
                    track_error(step.brain)
                except Exception:
                    pass
                if retries < max_retries:
                    logger.warning(
                        f"Step execution failed: {last_error}. "
                        f"Retrying... (Attempt {retries + 1}/{max_retries})"
                    )
                    await asyncio.sleep(1)
                    retries += 1
                    continue
                else:
                    logger.error(f"Step execution failed after {max_retries} retries: {last_error}")
                    break
            # Track successful request in global rate monitor
            try:
                from ...infra.rate_monitor import track_request
                track_request(step.brain)
            except Exception:
                pass
            output = resp.content or ""
            break
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as exc:
            last_error = str(exc)[:200]
            logger.warning(
                "conductor_step_exception",
                run_id=run_id,
                step=step.step,
                attempt=retries,
                error=last_error,
            )
            try:
                from ...infra.rate_monitor import track_error
                track_error(step.brain)
            except Exception:
                pass
            if retries < max_retries:
                logger.warning(
                    f"Step execution failed: {last_error}. "
                    f"Retrying... (Attempt {retries + 1}/{max_retries})"
                )
                await asyncio.sleep(1)
                retries += 1
            else:
                logger.error(f"Step execution failed after {max_retries} retries: {last_error}")
                break

    duration_ms = int((time.time() - start) * 1000)
    step.duration_ms = duration_ms

    # Cascade fallback: if primary brain failed, try next in _FREE_FALLBACK chain
    if not output:
        from ..router import _FREE_FALLBACK
        original_brain = step.brain
        fallback_brain_name = _FREE_FALLBACK.get(step.brain)
        while fallback_brain_name and not output:
            fallback_brain = router.get_brain(fallback_brain_name)
            if fallback_brain:
                logger.warning(
                    f"conductor_step_cascade_fallback: {step.brain} → {fallback_brain_name}",
                    run_id=run_id,
                    step=step.step,
                )
                try:
                    resp = await asyncio.wait_for(
                        fallback_brain.execute(prompt, timeout_seconds=timeout),
                        timeout=timeout + 10,
                    )
                    if not resp.is_error:
                        output = resp.content or ""
                        if output:
                            step.brain = fallback_brain_name
                            logger.info(f"Cascade success via {fallback_brain_name}")
                except Exception as exc:
                    logger.warning(f"Cascade fallback {fallback_brain_name} also failed: {exc}")
            fallback_brain_name = _FREE_FALLBACK.get(fallback_brain_name)
        if not output:
            step.brain = original_brain  # restore for error reporting

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
        step.error = last_error or "no output after retries exhausted"

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
