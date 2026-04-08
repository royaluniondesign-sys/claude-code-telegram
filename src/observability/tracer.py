"""Langfuse tracer — observability for brain executions.

Wraps the Langfuse SDK with graceful degradation:
- If Langfuse is unreachable, traces are silently dropped
- Thread-safe singleton pattern
- Zero overhead when disabled
"""

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Optional

import structlog

logger = structlog.get_logger()

# Singleton
_tracer: Optional["LangfuseTracer"] = None


@dataclass(frozen=True)
class TraceContext:
    """Immutable trace reference."""

    trace_id: str
    span_id: str = ""
    brain_name: str = ""
    start_time_ms: int = 0


class LangfuseTracer:
    """Langfuse observability client with graceful degradation."""

    def __init__(self) -> None:
        self._enabled = False
        self._client: Any = None
        self._init()

    def _init(self) -> None:
        """Initialize Langfuse client from environment."""
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host = os.getenv("LANGFUSE_HOST", "http://localhost:3001")

        if not public_key or not secret_key:
            logger.info("langfuse_disabled", reason="missing keys")
            return

        try:
            from langfuse import Langfuse  # type: ignore[import-untyped]

            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
                enabled=True,
            )
            self._enabled = True
            logger.info("langfuse_enabled", host=host)
        except ImportError:
            logger.warning("langfuse_disabled", reason="langfuse package not installed")
        except Exception as e:
            logger.error("langfuse_init_error", error=str(e))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def trace_brain(
        self,
        brain_name: str,
        user_id: int,
        message: str,
        intent: str = "",
        confidence: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TraceContext:
        """Start a trace for a brain execution.

        Returns a TraceContext (always — even if disabled, returns a no-op context).
        """
        now_ms = int(time.time() * 1000)

        if not self._enabled or not self._client:
            return TraceContext(trace_id="", brain_name=brain_name, start_time_ms=now_ms)

        try:
            trace = self._client.trace(
                name=f"brain:{brain_name}",
                user_id=str(user_id),
                input={"message": message[:500]},
                metadata={
                    "brain": brain_name,
                    "intent": intent,
                    "confidence": confidence,
                    **(metadata or {}),
                },
            )
            return TraceContext(
                trace_id=trace.id,
                brain_name=brain_name,
                start_time_ms=now_ms,
            )
        except Exception as e:
            logger.error("langfuse_trace_error", error=str(e))
            return TraceContext(trace_id="", brain_name=brain_name, start_time_ms=now_ms)

    def end_trace(
        self,
        ctx: TraceContext,
        output: str = "",
        error: str = "",
        cost: float = 0.0,
        duration_ms: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """End a trace with output/error."""
        if not self._enabled or not self._client or not ctx.trace_id:
            return

        try:
            elapsed = duration_ms or (int(time.time() * 1000) - ctx.start_time_ms)
            trace = self._client.trace(id=ctx.trace_id)

            update_data: Dict[str, Any] = {
                "output": {"response": output[:1000]} if output else {"error": error[:500]},
                "metadata": {
                    "duration_ms": elapsed,
                    "cost": cost,
                    "is_error": bool(error),
                    **(metadata or {}),
                },
            }

            if error:
                update_data["level"] = "ERROR"

            trace.update(**update_data)
        except Exception as e:
            logger.error("langfuse_end_trace_error", error=str(e))

    def log_generation(
        self,
        ctx: TraceContext,
        model: str,
        input_text: str = "",
        output_text: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Log an LLM generation event within a trace."""
        if not self._enabled or not self._client or not ctx.trace_id:
            return

        try:
            trace = self._client.trace(id=ctx.trace_id)
            trace.generation(
                name=f"llm:{model}",
                model=model,
                input=input_text[:500] if input_text else None,
                output=output_text[:1000] if output_text else None,
                usage={
                    "input": input_tokens,
                    "output": output_tokens,
                    "total": input_tokens + output_tokens,
                },
                metadata={"cost": cost},
            )
        except Exception as e:
            logger.error("langfuse_generation_error", error=str(e))

    @contextmanager
    def span(
        self, ctx: TraceContext, name: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """Context manager for trace spans."""
        span_data: Dict[str, Any] = {"name": name, "start_ms": int(time.time() * 1000)}

        if self._enabled and self._client and ctx.trace_id:
            try:
                trace = self._client.trace(id=ctx.trace_id)
                span = trace.span(name=name, metadata=metadata or {})
                span_data["span_id"] = span.id
            except Exception as e:
                logger.error("langfuse_span_error", error=str(e))

        try:
            yield span_data
        finally:
            if self._enabled and self._client and "span_id" in span_data:
                try:
                    elapsed = int(time.time() * 1000) - span_data["start_ms"]
                    span_data["duration_ms"] = elapsed
                except Exception:
                    pass

    def flush(self) -> None:
        """Flush pending traces (call on shutdown)."""
        if self._enabled and self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.error("langfuse_flush_error", error=str(e))

    def shutdown(self) -> None:
        """Shutdown client gracefully."""
        self.flush()
        if self._client:
            try:
                self._client.shutdown()
            except Exception:
                pass


def get_tracer() -> LangfuseTracer:
    """Get or create the global tracer singleton."""
    global _tracer
    if _tracer is None:
        _tracer = LangfuseTracer()
    return _tracer
