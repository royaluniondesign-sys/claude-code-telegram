"""Observability module — Langfuse tracing for multi-brain routing."""

from .tracer import LangfuseTracer, get_tracer

__all__ = ["LangfuseTracer", "get_tracer"]
