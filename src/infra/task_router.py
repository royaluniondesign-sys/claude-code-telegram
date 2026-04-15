"""Option C — Cognitive Task Router.

Decides whether an incoming task goes through:
  - Ruta A: Claude CLI + meta_context (conversational, simple queries)
  - Ruta B: Conductor 3-layer (complex tasks needing specialization)

The router itself is cognitive — it reads AURA's history of which route
produced better outcomes for which task types, and improves over time.

Decision flow:
  1. Fast heuristics (< 1ms) — obvious cases
  2. History-based confidence — learned from conductor_log.md
  3. local-ollama classifier for ambiguous cases (~2s, free)
  4. Fallback to Ruta A if uncertain (safe default)

Both routes write outcomes to the unified task memory so the router
learns from ALL tasks, not just self-improvement runs.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

_CONDUCTOR_LOG = Path.home() / ".aura" / "memory" / "conductor_log.md"

# ── Heuristics ────────────────────────────────────────────────────────────────

# Keywords that strongly suggest a complex multi-step task → Ruta B
_COMPLEX_KEYWORDS = frozenset({
    # Spanish
    "analiza", "análisis", "informe", "reporte", "estrategia", "plan",
    "implementa", "desarrolla", "construye", "crea", "diseña",
    "investiga", "investiga", "cliente", "propuesta", "campaña",
    "optimiza", "seo", "redacta", "documenta", "refactoriza",
    "arquitectura", "integra", "migra", "debug", "depura",
    # English
    "analyze", "report", "strategy", "plan", "implement", "develop",
    "build", "create", "design", "research", "client", "proposal",
    "campaign", "optimize", "document", "refactor", "architecture",
    "integrate", "migrate", "audit", "review", "generate",
})

# Patterns that strongly suggest a simple query → Ruta A
_SIMPLE_PATTERNS = [
    r"^(qué|que|cómo|como|cuál|cual|cuándo|cuando|dónde|donde|quién|quien)\b",
    r"^(what|how|when|where|who|why|is|are|can|does|do)\b",
    r"^(hola|hi|hello|ok|gracias|thanks|sí|si|no)\b",
    r"^[!$]",  # bash passthrough
    r"^/",     # command
]

# Intent values that have dedicated handlers — never route to conductor
_NATIVE_INTENTS = frozenset({"image", "video", "social", "email", "zero_token"})


@dataclass
class RouteDecision:
    """Result of task routing classification."""
    route: str          # "simple" | "complex"
    confidence: float   # 0.0 – 1.0
    reason: str         # human-readable explanation
    source: str         # "heuristic" | "history" | "llm" | "fallback"


# ── History-based learning ────────────────────────────────────────────────────

def _extract_task_type(text: str) -> str:
    """Normalize task text to a category for history matching."""
    text_lower = text.lower()
    if any(w in text_lower for w in ("seo", "keyword", "ranking", "google")):
        return "seo"
    if any(w in text_lower for w in ("código", "code", "function", "bug", "error", "fix")):
        return "code"
    if any(w in text_lower for w in ("cliente", "client", "propuesta", "proposal")):
        return "client"
    if any(w in text_lower for w in ("informe", "report", "análisis", "analyze")):
        return "analysis"
    if any(w in text_lower for w in ("email", "correo", "mensaje", "message")):
        return "communication"
    return "general"


def _historical_route_confidence(task_type: str) -> Optional[RouteDecision]:
    """Check conductor_log.md for historical success of each route for this task type.

    Returns a decision if history is strong enough (≥3 data points).
    """
    if not _CONDUCTOR_LOG.exists():
        return None

    try:
        text = _CONDUCTOR_LOG.read_text(errors="replace")
        # Look for entries matching this task type
        # Format: "**Task:** <title>" then "**Result:** ✅ COMMITTED | ..."
        pattern = rf"Task: [^\n]*{re.escape(task_type)}[^\n]*\n.*?Result: ([^\n]+)"
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)

        if len(matches) < 3:
            return None  # not enough history

        successes = sum(1 for m in matches if "COMMITTED" in m or "✅" in m)
        rate = successes / len(matches)

        if rate >= 0.7:
            return RouteDecision(
                route="complex",
                confidence=rate,
                reason=f"{task_type}: {successes}/{len(matches)} conductor runs succeeded",
                source="history",
            )
        elif rate < 0.3:
            return RouteDecision(
                route="simple",
                confidence=1.0 - rate,
                reason=f"{task_type}: conductor succeeded only {successes}/{len(matches)}, use CLI",
                source="history",
            )
        return None  # ambiguous — need LLM

    except Exception:
        return None


# ── LLM classifier ────────────────────────────────────────────────────────────

async def _llm_classify(task: str, brain_router: Any) -> Optional[RouteDecision]:
    """Use local-ollama to classify task complexity. ~2s, free."""
    try:
        if brain_router is None:
            return None
        ollama = brain_router.get_brain("local-ollama")
        if not ollama:
            return None

        prompt = f"""Classify this task as SIMPLE or COMPLEX.

SIMPLE: conversational question, quick fact, short answer, status check.
COMPLEX: needs research, multiple steps, generates a document/report/code, needs specialized tools.

Task: "{task[:300]}"

Reply with exactly one word: SIMPLE or COMPLEX"""

        import asyncio
        resp = await asyncio.wait_for(
            ollama.execute(prompt, timeout_seconds=15),
            timeout=18,
        )
        if resp.is_error or not resp.content:
            return None

        verdict = resp.content.strip().upper()
        if "COMPLEX" in verdict:
            return RouteDecision(
                route="complex",
                confidence=0.80,
                reason="local-ollama classified as complex task",
                source="llm",
            )
        elif "SIMPLE" in verdict:
            return RouteDecision(
                route="simple",
                confidence=0.80,
                reason="local-ollama classified as simple task",
                source="llm",
            )
        return None

    except Exception as e:
        logger.debug("task_router_llm_error", error=str(e))
        return None


# ── Main router ───────────────────────────────────────────────────────────────

async def classify_task(
    task: str,
    brain_router: Any = None,
    intent: Optional[Any] = None,
) -> RouteDecision:
    """Classify an incoming task and decide the routing path.

    Priority:
    1. Native intents (image/video/email) → always Ruta A (handled by dedicated paths)
    2. Fast heuristics → obvious simple/complex
    3. History-based confidence (≥3 data points) → learned preference
    4. local-ollama LLM classifier → for ambiguous cases
    5. Fallback → Ruta A (safe default)
    """
    # 1. Native intent → don't interfere with dedicated handlers
    if intent is not None:
        try:
            intent_val = intent.intent.value if hasattr(intent, 'intent') else str(intent)
            if intent_val in _NATIVE_INTENTS:
                return RouteDecision(
                    route="simple",
                    confidence=1.0,
                    reason=f"native intent handler: {intent_val}",
                    source="heuristic",
                )
        except Exception:
            pass

    text = task.strip()
    text_lower = text.lower()

    # 2a. Fast simple heuristics
    for pattern in _SIMPLE_PATTERNS:
        if re.match(pattern, text_lower):
            return RouteDecision(
                route="simple",
                confidence=0.95,
                reason=f"simple pattern match: {pattern[:30]}",
                source="heuristic",
            )

    # 2b. Short message → likely conversational
    if len(text) < 40:
        return RouteDecision(
            route="simple",
            confidence=0.85,
            reason=f"short message ({len(text)} chars)",
            source="heuristic",
        )

    # 2c. Complex keyword match
    words = set(re.findall(r'\b\w{4,}\b', text_lower))
    matched = words & _COMPLEX_KEYWORDS
    if len(matched) >= 2:
        return RouteDecision(
            route="complex",
            confidence=0.85,
            reason=f"complex keywords: {', '.join(list(matched)[:3])}",
            source="heuristic",
        )
    # Single complex keyword + long message → likely complex with lower confidence
    if len(matched) == 1 and len(text) >= 40:
        return RouteDecision(
            route="complex",
            confidence=0.72,
            reason=f"complex keyword '{next(iter(matched))}' + long message",
            source="heuristic",
        )

    # 3. History-based confidence
    task_type = _extract_task_type(text)
    hist = _historical_route_confidence(task_type)
    if hist:
        return hist

    # 4. LLM classifier (only for messages > 40 chars that weren't obvious)
    if len(text) > 40 and brain_router is not None:
        llm_decision = await _llm_classify(text, brain_router)
        if llm_decision:
            return llm_decision

    # 5. Fallback
    return RouteDecision(
        route="simple",
        confidence=0.60,
        reason="no clear signal — defaulting to Ruta A",
        source="fallback",
    )


# ── Outcome writer (unified memory for both routes) ───────────────────────────

def write_external_outcome(
    task: str,
    route: str,
    success: bool,
    duration_s: float,
    output_preview: str,
    run_id: str = "",
) -> None:
    """Write external task outcome to conductor_log.md in unified format.

    This closes the learning loop for Ruta A — when Claude CLI handles a task,
    the outcome is recorded in the same log that the conductor uses for Ruta B.
    The router learns from both.
    """
    try:
        from datetime import UTC, datetime
        _CONDUCTOR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        status = "✅ SUCCESS" if success else "❌ FAILED"
        entry = (
            f"\n## {ts} — ext-{run_id or int(time.time()) % 10000}\n"
            f"**Task:** {task[:80]}\n"
            f"**Route:** Ruta {'B (conductor)' if route == 'complex' else 'A (CLI)'}\n"
            f"**Result:** {status} | {duration_s:.1f}s\n"
            f"**Output:** {output_preview[:200]}\n"
        )
        with open(_CONDUCTOR_LOG, "a") as f:
            f.write(entry)
    except Exception as e:
        logger.debug("write_external_outcome_error", error=str(e))
