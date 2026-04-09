"""Brain Router — smart routing with free-tier cascade and pre-flight rate check.

Routing philosophy (CRITICAL):
  Claude (haiku/sonnet/opus) = PREMIUM TIER — reserved for complex, demanding tasks.
  All other brains = default routing, decided by complexity and availability.

Priority chain (cost-ascending, free → paid):
  1. zero-token  — bash/git/files: no LLM, instant
  2. gemini      — Google CLI, free: chat, search, translation, general
  3. openrouter  — HTTP free cascade: analysis, code (when API key set)
  4. cline       — local Ollama, $0: code editing
  5. codex       — ChatGPT subscription: code generation
  6. haiku       — Claude CLI, cheapest: moderate complexity + tool use
  7. sonnet      — Claude CLI, balanced: high complexity
  8. opus        — Claude CLI, deepest: architecture + massive tasks

Complexity gating (meta-router):
  score < 5  → free tier (gemini/openrouter/cline)
  score 5-14 → haiku (Claude, moderate complexity)
  score ≥ 15 → sonnet (Claude, high complexity)

Pre-flight: if selected brain is rate-limited, walk cascade until available brain found.
Full cascade: when a brain fails, try every next in chain until one succeeds.
"""

from typing import Any, Dict, List, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus
from .claude_brain import ClaudeBrain
from .executor_brain import ClineBrain, CodexBrain, OpenCodeBrain
from .gemini_brain import GeminiBrain
from .openrouter_brain import OpenRouterBrain
from ..economy.intent import Intent, IntentResult, classify
from ..economy.semantic_intent import classify_semantic

logger = structlog.get_logger()


def _has_openrouter_key() -> bool:
    """True if an OpenRouter API key is configured."""
    import os
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return True
    from pathlib import Path
    import json
    secrets = Path.home() / ".aura" / "secrets.json"
    if secrets.exists():
        try:
            data = json.loads(secrets.read_text())
            return bool(data.get("openrouter", {}).get("key"))
        except Exception:
            pass
    return False


# Full cascade chain — order determines failover priority
# Claude is deliberately at the END (premium tier, last resort unless complex)
_FULL_CASCADE: List[str] = [
    "gemini",      # free, fast, web-aware
    "openrouter",  # free HTTP cascade (needs API key for reliability)
    "cline",       # local Ollama, always free
    "codex",       # ChatGPT subscription
    "haiku",       # Claude cheapest — first Claude tier
    "sonnet",      # Claude balanced
    "opus",        # Claude deepest
]

# Intent → primary brain (free-first philosophy)
# EMAIL/CALENDAR use haiku because they need Claude's tool execution
_INTENT_BRAIN_MAP: Dict[Intent, str] = {
    Intent.BASH: "zero-token",
    Intent.FILES: "zero-token",
    Intent.GIT: "zero-token",
    Intent.SEARCH: "gemini",       # web search → gemini (has web access)
    Intent.TRANSLATE: "gemini",    # translation → gemini (fast + free)
    Intent.CHAT: "gemini",         # general chat → gemini first
    Intent.DEEP: "openrouter" if _has_openrouter_key() else "gemini",  # deep analysis
    Intent.CODE: "cline",          # code → local Ollama first (free, fast)
    Intent.EMAIL: "haiku",         # needs Claude tool: Resend API
    Intent.CALENDAR: "haiku",      # needs Claude tool: calendar read/write
}

# Per-brain next fallback (for quick single-step lookup)
_FREE_FALLBACK: Dict[str, str] = {
    "gemini": "openrouter",    # gemini fail → try openrouter
    "openrouter": "cline",     # openrouter fail/rate-limited → local cline
    "cline": "codex",          # cline offline → codex
    "codex": "haiku",          # codex fail → cheapest Claude
    "opencode": "cline",       # opencode broken → cline
    "haiku": "sonnet",         # haiku fail → sonnet
    "sonnet": "opus",          # sonnet fail → opus
}

# Complexity thresholds for escalating to Claude (from meta-router score)
_CLAUDE_SCORE_THRESHOLD = 5   # score ≥ 5 → haiku minimum
_OPUS_SCORE_THRESHOLD = 15    # score ≥ 15 → sonnet/opus


class BrainRouter:
    """Manages all brains with smart routing and rate-limit-aware cascading."""

    def __init__(self) -> None:
        self._brains: Dict[str, Brain] = {}
        self._active_brain: str = "haiku"
        # Per-user brain override (user_id -> brain_name)
        self._user_brains: Dict[int, str] = {}

        # Claude tiers (subscription CLI, no API key)
        self._brains["haiku"] = ClaudeBrain(model="haiku", timeout=60)
        self._brains["sonnet"] = ClaudeBrain(model="sonnet", timeout=180)
        self._brains["opus"] = ClaudeBrain(model="opus", timeout=300)

        # Sub-executor CLIs
        self._brains["opencode"] = OpenCodeBrain(timeout=300)  # free via OpenRouter
        self._brains["cline"] = ClineBrain(timeout=60)         # local Ollama, $0 (code edits only)
        self._brains["codex"] = CodexBrain(timeout=60)         # OpenAI subscription

        # Free HTTP brains (no subprocess overhead)
        self._brains["gemini"] = GeminiBrain(timeout=30)       # Google CLI, free
        self._brains["openrouter"] = OpenRouterBrain(timeout=45)  # OpenRouter free cascade

    def register_brain(self, name: str, brain: Brain) -> None:
        """Register an optional brain (e.g., API-based ones)."""
        self._brains[name] = brain
        logger.info("brain_registered", brain=name)

    @property
    def active_brain_name(self) -> str:
        return self._active_brain

    @property
    def available_brains(self) -> List[str]:
        return list(self._brains.keys())

    def get_brain(self, name: str) -> Optional[Brain]:
        return self._brains.get(name)

    def get_default_brain(self) -> Brain:
        return self._brains[self._active_brain]

    def set_active_brain(self, name: str, user_id: Optional[int] = None) -> bool:
        """Set active brain globally or per-user. Returns True if brain exists."""
        if name not in self._brains:
            return False
        if user_id is not None:
            self._user_brains[user_id] = name
            logger.info("brain_switched_user", user_id=user_id, brain=name)
        else:
            self._active_brain = name
            logger.info("brain_switched_global", brain=name)
        return True

    def get_active_brain(self, user_id: Optional[int] = None) -> Brain:
        if user_id is not None and user_id in self._user_brains:
            name = self._user_brains[user_id]
        else:
            name = self._active_brain
        return self._brains[name]

    def get_active_brain_name(self, user_id: Optional[int] = None) -> str:
        if user_id is not None and user_id in self._user_brains:
            return self._user_brains[user_id]
        return self._active_brain

    def reset_user_brain(self, user_id: int) -> None:
        self._user_brains.pop(user_id, None)

    async def health_check_all(self) -> Dict[str, BrainStatus]:
        results = {}
        for name, brain in self._brains.items():
            try:
                results[name] = await brain.health_check()
            except Exception as e:
                logger.error("brain_health_error", brain=name, error=str(e))
                results[name] = BrainStatus.ERROR
        return results

    def smart_route(
        self,
        message: str,
        user_id: Optional[int] = None,
        rate_monitor: Any = None,
    ) -> tuple:
        """Classify message intent and route to optimal brain.

        With rate_monitor: skips rate-limited brains and cascades to fallback
        before even sending the request.

        Returns (brain_name, intent_result).
        """
        import re as _re
        intent = classify_semantic(message)

        # Zero-token always wins — bash, git, file commands (no LLM)
        if intent.intent in (Intent.BASH, Intent.FILES, Intent.GIT):
            return "zero-token", intent

        # URL + analysis keywords → force gemini (web-aware, can fetch/analyze URLs)
        _has_url = bool(_re.search(r"https?://\S+", message))
        _analysis_kw = bool(_re.search(
            r"(?i)\b(analiz|optimiz|revis|check|inspect|audit|seo|perform|web|site|page)\w*\b",
            message,
        ))
        if _has_url and _analysis_kw and intent.intent not in (Intent.BASH, Intent.FILES, Intent.GIT):
            logger.debug("smart_route_url_analysis", url=True, forcing="gemini")
            target = "gemini"
            if target in self._brains:
                return target, intent

        # User explicit lock via /brain X takes priority
        if user_id is not None and user_id in self._user_brains:
            locked = self._user_brains[user_id]
            logger.debug("smart_route_user_lock", user_id=user_id, locked=locked,
                         intent=intent.intent.value)
            return locked, intent

        # Explicit CLI override from intent pattern (usa opencode/cline/codex)
        if intent.suggested_brain in self._brains and intent.confidence >= 1.0:
            logger.debug("smart_route_explicit_cli", brain=intent.suggested_brain,
                         intent=intent.intent.value)
            return intent.suggested_brain, intent

        # Route by intent — free-first philosophy
        target = _INTENT_BRAIN_MAP.get(intent.intent, "gemini")
        if target == "zero-token":
            return "zero-token", intent

        # ── Complexity gate: meta-router score → escalate to Claude if needed ──
        try:
            from ..claude.meta_router import route_request as _meta_route, ModelTier
            decision = _meta_route(message, urgent=False)
            if decision.tier == ModelTier.OPUS and target not in ("sonnet", "opus"):
                logger.info("meta_router_escalate_opus", from_brain=target, score=decision.score)
                target = "sonnet"  # sonnet before opus, let cascade handle it
            elif decision.tier == ModelTier.SONNET and target not in ("haiku", "sonnet", "opus"):
                logger.info("meta_router_escalate_haiku", from_brain=target, score=decision.score)
                target = "haiku"  # complex task → skip free tier, go straight to Claude
        except Exception:
            pass  # meta-router failure is non-fatal

        # ── Pre-flight: walk cascade until we find an available brain ──
        visited: set = set()
        original_target = target
        while target and target not in visited and target in self._brains:
            visited.add(target)
            if rate_monitor:
                usage = rate_monitor.get_usage(target)
                if usage.is_rate_limited:
                    next_brain = _FREE_FALLBACK.get(target)
                    # Walk _FULL_CASCADE for next available after target
                    if not next_brain:
                        idx = _FULL_CASCADE.index(target) if target in _FULL_CASCADE else -1
                        next_brain = next(
                            (b for b in _FULL_CASCADE[idx+1:] if b not in visited),
                            None
                        )
                    if next_brain and next_brain not in visited:
                        logger.info("preflight_skip_ratelimited",
                                    skipped=target, next=next_brain,
                                    recover_in=usage.recover_in_str)
                        target = next_brain
                        continue
                    # All cascade options exhausted → fall back to haiku
                    target = "haiku"
                    break
            break  # target is available

        if target in self._brains:
            logger.debug("smart_route_intent", brain=target,
                         intent=intent.intent.value, original=original_target)
            return target, intent

        # Final fallback: haiku (always available via Claude subscription)
        return "haiku", intent

    def get_cascade_chain(self, failed_brain: str) -> List[str]:
        """Return ordered list of brains to try after failed_brain.

        Starts from failed_brain's position in _FULL_CASCADE and returns
        everything after it. Used by orchestrator for multi-level cascade.
        """
        try:
            idx = _FULL_CASCADE.index(failed_brain)
            return _FULL_CASCADE[idx + 1:]
        except ValueError:
            return ["haiku", "sonnet"]  # safe fallback

    def get_fallback_brain(self, failed_brain: str,
                           intent: Optional[Intent] = None,
                           rate_monitor: Any = None) -> Optional[str]:
        """Return next brain in cascade after failed_brain.

        Uses _FULL_CASCADE ordering (free → paid). Skips rate-limited brains
        if rate_monitor is provided.
        """
        chain = self.get_cascade_chain(failed_brain)
        for candidate in chain:
            if candidate not in self._brains:
                continue
            if rate_monitor:
                try:
                    usage = rate_monitor.get_usage(candidate)
                    if usage.is_rate_limited:
                        logger.debug("fallback_skip_ratelimited", skipped=candidate)
                        continue
                except Exception:
                    pass
            return candidate
        return None

    async def get_all_info(self) -> List[Dict[str, Any]]:
        infos = []
        for name, brain in self._brains.items():
            try:
                info = await brain.get_info()
                status = await brain.health_check()
                info["status"] = status.value
                info["emoji"] = brain.emoji
                info["is_active"] = name == self._active_brain
                infos.append(info)
            except Exception as e:
                infos.append({
                    "name": name,
                    "display_name": brain.display_name,
                    "emoji": brain.emoji,
                    "status": "error",
                    "error": str(e),
                    "is_active": name == self._active_brain,
                })
        return infos
