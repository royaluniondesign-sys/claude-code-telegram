"""AURA Cortex — self-learning meta-orchestration layer.

Wraps the brain router with a learned performance model. Every interaction
updates the cortex. Over time, routing improves automatically.

Data lives at ~/.aura/cortex.json and persists across sessions.

Brain score model:
  score = EMA(success_rate) * speed_factor
  speed_factor = 1.0 / (1.0 + avg_latency_s / 10.0)  # penalize slow brains
  EMA alpha = 0.15  (15% new data, 85% history)

Error pattern memory:
  If brain X fails 2+ times for intent Y → create bypass rule
  Bypass rule: for intent Y, skip brain X in cascade

Session context:
  Tracks last 10 intents + topics worked on (from prompt keywords)
  Used to enrich prompts automatically
"""

import os
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# EMA parameters
EMA_ALPHA = 0.15  # 15% new data, 85% history

# Error pattern threshold — after N failures, create bypass rule
BYPASS_THRESHOLD = 2

# Paths
_CORTEX_PATH = Path.home() / ".aura" / "cortex.json"
_CORTEX_TMP = Path.home() / ".aura" / ".cortex.json.tmp"

# Cascade order for picking bypass brain
_CASCADE_ORDER = [
    "ollama-rud", "haiku", "gemini", "openrouter", "cline", "codex", "sonnet", "opus"
]

# Keywords for topic extraction
_TOPIC_RE = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9_\-]{3,})\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset([
    "that", "this", "with", "from", "have", "will", "what", "when",
    "where", "there", "their", "which", "about", "into", "your", "more",
    "also", "some", "been", "como", "para", "esto", "esta", "tiene",
    "hacer", "puede", "quiero", "necesito", "favor",
])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _extract_topics(text: str, max_topics: int = 5) -> List[str]:
    """Extract meaningful keywords from prompt text."""
    words = _TOPIC_RE.findall(text)
    seen: Dict[str, int] = {}
    for w in words:
        w_lower = w.lower()
        if w_lower not in _STOPWORDS and len(w_lower) >= 4:
            seen[w_lower] = seen.get(w_lower, 0) + 1
    # Sort by frequency, take top N
    ranked = sorted(seen.keys(), key=lambda k: -seen[k])
    return ranked[:max_topics]


def _empty_data() -> Dict[str, Any]:
    """Return a fresh, empty cortex data structure."""
    return {
        "version": 1,
        "brain_scores": {},
        "error_patterns": [],
        "session_context": {
            "last_intent": "",
            "last_brain": "",
            "recent_topics": [],
            "recent_intents": [],
        },
        "total_interactions": 0,
        "last_updated": _now_iso(),
    }


class AuraCortex:
    """Self-learning orchestration layer that wraps BrainRouter.

    Learns from every interaction: tracks latency, success rate, and error
    patterns per brain per intent. Routes around known failure patterns
    automatically. Never crashes the bot — all failures are caught and logged.
    """

    def __init__(self, router: Any) -> None:
        self._router = router
        self.data: Dict[str, Any] = _empty_data()
        self._load()
        logger.info(
            "cortex_initialized",
            total_interactions=self.data.get("total_interactions", 0),
            bypass_rules=len(self.data.get("error_patterns", [])),
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def route(
        self,
        message: str,
        user_id: Optional[int],
        rate_monitor: Any = None,
        urgent: bool = False,
    ) -> tuple:
        """Route a message via router.smart_route, applying learned bypass rules.

        Returns (brain_name, intent_result).
        Falls back to router.smart_route transparently on any error.
        """
        try:
            routed_brain, intent = self._router.smart_route(
                message, user_id, rate_monitor=rate_monitor, urgent=urgent
            )

            # Resolve the intent string for bypass lookup
            intent_str = ""
            try:
                intent_str = intent.intent.value if hasattr(intent, "intent") else str(intent)
            except Exception:
                pass

            # Check if there's a learned bypass for this brain+intent combo
            if intent_str and routed_brain not in ("zero-token",):
                bypass = self._check_error_pattern(routed_brain, intent_str)
                if bypass and bypass in self._router._brains:
                    # Verify the bypass brain is not rate-limited
                    bypass_ok = True
                    if rate_monitor:
                        try:
                            usage = rate_monitor.get_usage(bypass)
                            if usage.is_rate_limited:
                                bypass_ok = False
                        except Exception:
                            pass
                    if bypass_ok:
                        logger.info(
                            "cortex_bypass_applied",
                            original=routed_brain,
                            bypass=bypass,
                            intent=intent_str,
                        )
                        return bypass, intent

            return routed_brain, intent

        except Exception as exc:
            logger.warning("cortex_route_error", error=str(exc))
            # Transparent fallback — use router directly, never return None intent
            try:
                return self._router.smart_route(
                    message, user_id, rate_monitor=rate_monitor, urgent=urgent
                )
            except Exception as exc2:
                logger.error("cortex_route_fallback_error", error=str(exc2))
                # Return safe sentinel — IntentResult-like object
                from ..economy.intent import IntentResult, Intent
                safe = IntentResult(
                    intent=Intent.CHAT,
                    confidence=0.5,
                    suggested_brain="haiku",
                    reason="cortex_fallback",
                )
                return "haiku", safe

    def record_outcome(
        self,
        brain: str,
        intent: str,
        success: bool,
        duration_ms: int,
        error: str = "",
        prompt: str = "",
    ) -> None:
        """Record the outcome of a brain call and update learned scores.

        Args:
            brain: Brain name (e.g. "haiku", "gemini")
            intent: Intent value string (e.g. "chat", "code", "search")
            success: Whether the call succeeded
            duration_ms: Call duration in milliseconds
            error: Error string if not success (optional)
            prompt: Original prompt for topic extraction (optional)
        """
        try:
            self._update_brain_score(brain, intent, success, duration_ms)
            if not success:
                self._check_and_create_bypass(brain, intent, error)
            self._update_session_context(intent, brain, prompt)
            self.data["total_interactions"] = self.data.get("total_interactions", 0) + 1
            self.data["last_updated"] = _now_iso()
            self._save()
        except Exception as exc:
            logger.warning("cortex_record_error", error=str(exc))

    def get_context_summary(self) -> str:
        """Return a brief human-readable context string for prompt enrichment."""
        try:
            ctx = self.data.get("session_context", {})
            topics = ctx.get("recent_topics", [])
            last_intent = ctx.get("last_intent", "")
            parts = []
            if topics:
                parts.append("Trabajando en: " + ", ".join(topics[:4]))
            if last_intent:
                parts.append(f"Intento reciente: {last_intent}")
            return ". ".join(parts) if parts else ""
        except Exception:
            return ""

    def get_status(self) -> Dict[str, Any]:
        """Return full cortex state for dashboard display."""
        try:
            brain_scores = self.data.get("brain_scores", {})
            error_patterns = self.data.get("error_patterns", [])

            # Best brain per intent
            best_by_intent: Dict[str, Dict[str, Any]] = {}
            for brain_name, intents in brain_scores.items():
                for intent_name, stats in intents.items():
                    score = stats.get("score", 0.0)
                    current_best = best_by_intent.get(intent_name)
                    if current_best is None or score > current_best.get("score", 0.0):
                        best_by_intent[intent_name] = {
                            "brain": brain_name,
                            "score": round(score, 3),
                            "samples": stats.get("samples", 0),
                            "avg_latency_ms": stats.get("avg_latency_ms", 0),
                        }

            # Active bypasses summary
            bypasses = [
                {
                    "from": p["brain"],
                    "intent": p["intent"],
                    "to": p.get("bypass_to", "haiku"),
                    "failures": p.get("count", 0),
                    "note": p.get("note", ""),
                }
                for p in error_patterns
            ]

            return {
                "total_interactions": self.data.get("total_interactions", 0),
                "last_updated": self.data.get("last_updated", ""),
                "learned_rules": len(error_patterns),
                "best_by_intent": best_by_intent,
                "active_bypasses": bypasses,
                "session_context": self.data.get("session_context", {}),
                "brain_scores_summary": {
                    brain: {
                        intent: {
                            "score": round(s.get("score", 0), 3),
                            "samples": s.get("samples", 0),
                            "errors": s.get("errors", 0),
                        }
                        for intent, s in intents.items()
                    }
                    for brain, intents in brain_scores.items()
                },
            }
        except Exception as exc:
            logger.warning("cortex_status_error", error=str(exc))
            return {
                "total_interactions": self.data.get("total_interactions", 0),
                "error": str(exc),
            }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _update_brain_score(
        self, brain: str, intent: str, success: bool, duration_ms: int
    ) -> None:
        """Update EMA score for brain+intent pair."""
        scores = self.data.setdefault("brain_scores", {})
        brain_data = scores.setdefault(brain, {})
        intent_data = brain_data.setdefault(
            intent,
            {"score": 0.8, "samples": 0, "avg_latency_ms": duration_ms, "errors": 0},
        )

        samples = intent_data.get("samples", 0)
        old_score = intent_data.get("score", 0.8)
        old_latency = intent_data.get("avg_latency_ms", duration_ms)

        # EMA for success rate (1.0 = success, 0.0 = failure)
        new_sample = 1.0 if success else 0.0
        success_ema = EMA_ALPHA * new_sample + (1 - EMA_ALPHA) * old_score

        # EMA for latency
        latency_ema = EMA_ALPHA * duration_ms + (1 - EMA_ALPHA) * old_latency

        # Combined score: success_rate * speed_factor
        speed_factor = 1.0 / (1.0 + latency_ema / 10000.0)
        combined = success_ema * speed_factor

        # Immutable update (new dict)
        brain_data[intent] = {
            "score": combined,
            "samples": samples + 1,
            "avg_latency_ms": int(latency_ema),
            "errors": intent_data.get("errors", 0) + (0 if success else 1),
        }

    def _check_and_create_bypass(self, brain: str, intent: str, error: str) -> None:
        """Check if error threshold reached; create bypass rule if so."""
        brain_scores = self.data.get("brain_scores", {})
        brain_intent_data = brain_scores.get(brain, {}).get(intent, {})
        error_count = brain_intent_data.get("errors", 0)

        if error_count < BYPASS_THRESHOLD:
            return  # Not enough failures yet

        patterns = self.data.setdefault("error_patterns", [])

        # Check if bypass already exists for this brain+intent
        for pattern in patterns:
            if pattern.get("brain") == brain and pattern.get("intent") == intent:
                # Update count only
                pattern["count"] = error_count
                pattern["last_error"] = error[:200] if error else ""
                return

        # New bypass rule — pick next available brain in cascade
        bypass_to = self._pick_bypass_brain(brain)
        note = f"Auto-bypass: {brain} failed {error_count}x for {intent}"
        if error:
            note += f" (last: {error[:80]})"

        # Immutable append: create new list with new entry
        new_pattern = {
            "brain": brain,
            "intent": intent,
            "count": error_count,
            "bypass_to": bypass_to,
            "created": _now_iso(),
            "note": note,
            "last_error": error[:200] if error else "",
        }
        self.data["error_patterns"] = list(patterns) + [new_pattern]
        logger.info(
            "cortex_bypass_created",
            brain=brain,
            intent=intent,
            bypass_to=bypass_to,
            failures=error_count,
        )

    def _pick_bypass_brain(self, failed_brain: str) -> str:
        """Pick the next available brain in cascade after failed_brain."""
        available = set(self._router._brains.keys()) if self._router else set()
        try:
            idx = _CASCADE_ORDER.index(failed_brain)
        except ValueError:
            idx = -1
        for candidate in _CASCADE_ORDER[idx + 1:]:
            if candidate in available:
                return candidate
        return "haiku"

    def _check_error_pattern(self, brain: str, intent: str) -> Optional[str]:
        """Return bypass brain if a learned bypass pattern exists, else None."""
        for pattern in self.data.get("error_patterns", []):
            if pattern.get("brain") == brain and pattern.get("intent") == intent:
                return pattern.get("bypass_to")
        return None

    def _update_session_context(self, intent: str, brain: str, prompt: str) -> None:
        """Update session context with latest intent and extracted topics."""
        ctx = self.data.setdefault("session_context", {
            "last_intent": "",
            "last_brain": "",
            "recent_topics": [],
            "recent_intents": [],
        })

        # Immutable list updates (cap at 10)
        recent_intents = list(ctx.get("recent_intents", []))
        recent_intents.insert(0, intent)
        recent_intents = recent_intents[:10]

        recent_topics = list(ctx.get("recent_topics", []))
        if prompt:
            new_topics = _extract_topics(prompt)
            # Merge: put new topics first, deduplicate, cap at 10
            merged = list(dict.fromkeys(new_topics + recent_topics))[:10]
            recent_topics = merged

        self.data["session_context"] = {
            "last_intent": intent,
            "last_brain": brain,
            "recent_topics": recent_topics,
            "recent_intents": recent_intents,
        }

    def _save(self) -> None:
        """Thread-safe atomic save: write to tmp then rename."""
        try:
            _CORTEX_TMP.parent.mkdir(parents=True, exist_ok=True)
            _CORTEX_TMP.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(_CORTEX_TMP), str(_CORTEX_PATH))
        except Exception as exc:
            logger.warning("cortex_save_error", error=str(exc))

    def _load(self) -> None:
        """Load cortex data from file. Gracefully handles missing or corrupt file."""
        if not _CORTEX_PATH.exists():
            logger.debug("cortex_no_file", path=str(_CORTEX_PATH))
            return
        try:
            raw = _CORTEX_PATH.read_text(encoding="utf-8")
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                # Merge loaded data into defaults (preserve any new keys)
                defaults = _empty_data()
                defaults.update(loaded)
                self.data = defaults
                logger.debug(
                    "cortex_loaded",
                    interactions=self.data.get("total_interactions", 0),
                    bypasses=len(self.data.get("error_patterns", [])),
                )
        except Exception as exc:
            logger.warning("cortex_load_error", error=str(exc))
            # Keep empty defaults — don't crash
