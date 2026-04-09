"""Brain Router — smart routing with free-tier cascade and pre-flight rate check.

Routing priority (cost-ascending):
  1. zero-token  — bash, git, file commands (no LLM)
  2. gemini      — CLI: chat, deep, search, translation (Google free)
  3. openrouter  — HTTP: code, complex tasks (OpenRouter free model cascade)
  4. cline       — local Ollama, $0 (code edits)
  5. codex       — OpenAI subscription (code gen)
  6. haiku       — cheapest Claude CLI (subscription)
  7. sonnet      — main Claude (subscription)
  8. opus        — deepest Claude (subscription, rare)

Pre-flight check: if the selected brain is rate-limited, cascade to fallback
before even sending the request.
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

# Intent → brain mapping (free-tier first, Claude only when tools are needed)
#
# Gemini CLI = full code assistant with web tools — ONLY use for SEARCH.
#   - For chat/email it runs msmtp, creates files, loops for minutes.
# OpenRouter = simple HTTP LLM — fast, free, no tool execution.
# Haiku = Claude CLI — has Bash/tool access, subscription, $0 on Max.
_INTENT_BRAIN_MAP: Dict[Intent, str] = {
    Intent.BASH: "zero-token",
    Intent.FILES: "zero-token",
    Intent.GIT: "zero-token",
    Intent.SEARCH: "gemini",       # Gemini CLI: web-aware, fast for queries
    Intent.TRANSLATE: "openrouter",  # simple text transform, no tools needed
    Intent.CHAT: "openrouter",     # fast HTTP, no subprocess
    Intent.DEEP: "openrouter",     # analysis via free model cascade
    Intent.CODE: "openrouter",     # code gen via free model cascade
    Intent.EMAIL: "haiku",         # needs Claude tools to compose + send via Resend
    Intent.CALENDAR: "haiku",      # needs Claude tools to read/write calendar
}

# Claude escalation chain — only after all free tiers exhausted
_FALLBACK_CHAIN: List[str] = ["haiku", "sonnet", "opus"]

# Per-intent fallback when primary fails (before escalating to Claude)
# NOTE: openrouter → haiku (NOT cline) for DEEP/CHAT tasks.
# Cline is a code editor (qwen2.5:7b local) — collapses on analysis/web tasks.
_FREE_FALLBACK: Dict[str, str] = {
    "gemini": "openrouter",     # gemini CLI fail/timeout → openrouter HTTP
    "openrouter": "haiku",      # openrouter rate-limited → cheapest Claude CLI
    "cline": "haiku",           # cline offline/fail → cheapest Claude
    "codex": "openrouter",      # codex broken → openrouter
    "opencode": "openrouter",   # opencode broken → openrouter
    "haiku": "sonnet",          # haiku fails → sonnet
}

# Intents where cline IS a valid intermediate fallback (code editing only)
_CLINE_ELIGIBLE_INTENTS = {Intent.CODE}


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

        # Route by intent — cheapest capable brain first
        target = _INTENT_BRAIN_MAP.get(intent.intent, "haiku")
        if target == "zero-token":
            return "zero-token", intent

        # Pre-flight: cascade past rate-limited brains before even trying
        if rate_monitor and target in self._brains:
            visited: set = set()
            while target not in visited and target in self._brains:
                visited.add(target)
                usage = rate_monitor.get_usage(target)
                if not usage.is_rate_limited:
                    break
                # Brain is rate-limited — skip to fallback
                fallback = _FREE_FALLBACK.get(target) or self.get_fallback_brain(target)
                if not fallback or fallback in visited:
                    break
                logger.info("preflight_skip_ratelimited", skipped=target, next=fallback)
                target = fallback

        if target in self._brains:
            logger.debug("smart_route_intent", brain=target, intent=intent.intent.value,
                         confidence=intent.confidence)
            return target, intent

        # Fallback to haiku
        return self._active_brain, intent

    def get_fallback_brain(self, failed_brain: str,
                           intent: Optional[Intent] = None) -> Optional[str]:
        """Escalate to next brain when one fails or is rate-limited.

        Free-tier cascade: gemini/openrouter → haiku → sonnet → opus
        Cline only in the chain for CODE intent.
        """
        # Check per-brain fallback map first
        if failed_brain in _FREE_FALLBACK:
            candidate = _FREE_FALLBACK[failed_brain]
            if candidate in self._brains:
                return candidate

        # Claude escalation chain
        chain = _FALLBACK_CHAIN
        try:
            idx = chain.index(failed_brain)
        except ValueError:
            # Unknown brain — go to haiku as safe default
            return "haiku" if "haiku" in self._brains else None
        for candidate in chain[idx + 1:]:
            if candidate in self._brains:
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
