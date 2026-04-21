"""Conductor handler — routes complex tasks through the 3-layer conductor.

Contains:
  _handle_conductor_task — complex task → Conductor orchestration
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from ..orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class ConductorHandlerMixin:
    """Mixin providing 3-layer conductor task routing."""

    async def _handle_conductor_task(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        task: str,
        user_id: int,
        route_decision: Any,
    ) -> None:
        """Ruta B — Route complex external task through the 3-layer conductor."""
        import time as _time
        from src.infra.task_router import write_external_outcome

        progress_msg = await update.message.reply_text(
            f"🧠 <b>Conductor</b> — analizando tarea compleja…\n"
            f"<i>{route_decision.reason}</i>",
            parse_mode="HTML",
        )
        t_start = _time.time()

        try:
            from src.brains.conductor import get_conductor, Conductor
            conductor = get_conductor(router)
            if conductor is None:
                conductor = Conductor(router, notify_fn=None)

            # Live progress via SSE events — update Telegram message
            last_update = _time.time()

            async def _progress_edit(text: str) -> None:
                nonlocal last_update
                if _time.time() - last_update < 8:  # max 1 edit per 8s (flood protection)
                    return
                last_update = _time.time()
                try:
                    await progress_msg.edit_text(text, parse_mode="HTML")
                except Exception:
                    pass

            conductor._notify = lambda msg: _progress_edit(
                f"🧠 <b>Conductor</b> — {msg[:200]}"
            )

            result = await asyncio.wait_for(
                conductor.run(task, source="external"),
                timeout=240,
            )

            duration_s = round(_time.time() - t_start, 1)
            output = result.final_output.strip() if result.final_output else ""

            if result.is_error or not output:
                await progress_msg.edit_text(
                    f"❌ Conductor no produjo output ({result.steps_failed} pasos fallaron)\n"
                    f"Tiempo: {duration_s}s",
                    parse_mode="HTML",
                )
                write_external_outcome(
                    task=task[:80],
                    route="complex",
                    success=False,
                    duration_s=duration_s,
                    output_preview=f"{result.steps_failed} steps failed",
                )
                return

            # Delete progress, send real answer
            try:
                await progress_msg.delete()
            except Exception:
                pass

            # Split long outputs (Telegram 4096 char limit)
            chunk_size = 3800
            chunks = [output[i:i + chunk_size] for i in range(0, len(output), chunk_size)]
            for i, chunk in enumerate(chunks):
                prefix = (
                    f"<b>🧠 Conductor</b> ({duration_s}s · {result.steps_completed}✓)\n\n"
                    if i == 0
                    else ""
                )
                await update.message.reply_text(prefix + chunk, parse_mode="HTML")

            write_external_outcome(
                task=task[:80],
                route="complex",
                success=True,
                duration_s=duration_s,
                output_preview=output[:200],
            )
            logger.info(
                "conductor_external_task_done",
                user_id=user_id,
                duration_s=duration_s,
                steps_ok=result.steps_completed,
                confidence=route_decision.confidence,
            )

        except asyncio.TimeoutError:
            try:
                await progress_msg.edit_text(
                    "⏱️ Conductor timeout (240s) — tarea muy larga. Intenta dividirla.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            write_external_outcome(
                task=task[:80],
                route="complex",
                success=False,
                duration_s=240.0,
                output_preview="timeout",
            )
        except Exception as exc:
            logger.error("conductor_external_task_error", error=str(exc), user_id=user_id)
            try:
                await progress_msg.edit_text(
                    f"❌ Error en conductor: {str(exc)[:200]}", parse_mode="HTML"
                )
            except Exception:
                pass
            write_external_outcome(
                task=task[:80],
                route="complex",
                success=False,
                duration_s=round(time.time() - t_start, 1),
                output_preview=str(exc)[:100],
            )
