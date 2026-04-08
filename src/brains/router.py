"""Brain Router — Haiku→Sonnet→Opus escalation with Gemini for search.

Routing priority (cost-ascending):
  1. zero-token  — bash, git, file commands (no LLM)
  2. gemini      — internet search, URLs, translation (free tier)
  3. haiku       — CHAT, DEEP, simple tasks (cheapest Claude, subscription)
  4. sonnet      — CODE, complex reasoning (subscription)
  5. opus        — deep architecture, max capability (subscription, rare)

All Claude tiers use the `claude` CLI (Max subscription, no API key).
Gemini uses the free tier API key.
"""

from typing import Any, Dict, List, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus
from .claude_brain import ClaudeBrain
from .executor_brain import ClineBrain, CodexBrain, OpenCodeBrain
from .gemini_brain import GeminiBrain
from ..economy.intent import Intent, IntentResult, classify

logger = structlog.get_logger()

# Intent → brain mapping (cheapest brain that can handle the intent)
_INTENT_BRAIN_MAP: Dict[Intent, str] = {
    Intent.BASH: "zero-token",
    Intent.FILES: "zero-token",
    Intent.GIT: "zero-token",
    Intent.SEARCH: "gemini",
    Intent.TRANSLATE: "gemini",
    Intent.EMAIL: "gemini",
    Intent.CALENDAR: "gemini",
    Intent.CHAT: "haiku",
    Intent.DEEP: "haiku",
    Intent.CODE: "sonnet",  # Code needs tool-capable Sonnet
}

# Escalation chain — if a brain fails, try the next one
_FALLBACK_CHAIN: List[str] = ["haiku", "sonnet", "opus"]


class BrainRouter:
    """Manages Claude Haiku→Sonnet→Opus + Gemini, routes by intent."""

    def __init__(self) -> None:
        self._brains: Dict[str, Brain] = {}
        self._active_brain: str = "haiku"
        # Per-user brain override (user_id -> brain_name)
        self._user_brains: Dict[int, str] = {}

        # Claude tiers (subscription, no API key)
        self._brains["haiku"] = ClaudeBrain(model="haiku", timeout=60)
        self._brains["sonnet"] = ClaudeBrain(model="sonnet", timeout=180)
        self._brains["opus"] = ClaudeBrain(model="opus", timeout=300)

        # Sub-executor CLIs (delegated by Claude or direct routing)
        self._brains["opencode"] = OpenCodeBrain(timeout=300)  # free tier
        self._brains["cline"] = ClineBrain(timeout=300)        # local Ollama, $0
        self._brains["codex"] = CodexBrain(timeout=180)        # OpenAI sub

        # Gemini for internet queries (free tier)
        self._brains["gemini"] = GeminiBrain()

    def register_brain(self, name: str, brain: Brain) -> None:
        """Register an optional brain (e.g., API-based ones)."""
        self._brains[name] = brain
        logger.info("brain_registered", brain=name)

    @property
    def active_brain_name(self) -> str:
        """Default active brain name."""
        return self._active_brain

    @property
    def available_brains(self) -> List[str]:
        """List registered brain names."""
        return list(self._brains.keys())

    def get_brain(self, name: str) -> Optional[Brain]:
        """Get a brain by name."""
        return self._brains.get(name)

    def get_default_brain(self) -> Brain:
        """Get the default brain (Gemini)."""
        return self._brains[self._active_brain]

    def set_active_brain(self, name: str, user_id: Optional[int] = None) -> bool:
        """Set active brain globally or per-user.

        Returns True if the brain exists, False otherwise.
        """
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
        """Get the active brain for a user (falls back to global default)."""
        if user_id is not None and user_id in self._user_brains:
            name = self._user_brains[user_id]
        else:
            name = self._active_brain
        return self._brains[name]

    def get_active_brain_name(self, user_id: Optional[int] = None) -> str:
        """Get active brain name for a user."""
        if user_id is not None and user_id in self._user_brains:
            return self._user_brains[user_id]
        return self._active_brain

    def reset_user_brain(self, user_id: int) -> None:
        """Reset user to global default brain."""
        self._user_brains.pop(user_id, None)

    async def health_check_all(self) -> Dict[str, BrainStatus]:
        """Check health of all brains."""
        results = {}
        for name, brain in self._brains.items():
            try:
                results[name] = await brain.health_check()
            except Exception as e:
                logger.error("brain_health_error", brain=name, error=str(e))
                results[name] = BrainStatus.ERROR
        return results

    def smart_route(self, message: str, user_id: Optional[int] = None) -> tuple:
        """Classify message intent and route to optimal brain.

        Returns (brain_name, intent_result).
        Priority: zero-token > user lock > intent map > default (haiku).
        """
        intent = classify(message)

        # Zero-token always wins — bash, git, file commands (no LLM)
        if intent.intent in (Intent.BASH, Intent.FILES, Intent.GIT):
            return "zero-token", intent

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

        # Route by intent — cheapest capable brain
        target = _INTENT_BRAIN_MAP.get(intent.intent, "haiku")
        if target == "zero-token":
            return "zero-token", intent
        if target in self._brains:
            logger.debug("smart_route_intent", brain=target, intent=intent.intent.value,
                         confidence=intent.confidence)
            return target, intent

        # Fallback to haiku
        return self._active_brain, intent

    def get_fallback_brain(self, failed_brain: str) -> Optional[str]:
        """Escalate to next Claude tier when one fails or is rate-limited.

        Chain: haiku → sonnet → opus → gemini (last resort).
        """
        chain = _FALLBACK_CHAIN + ["gemini"]
        try:
            idx = chain.index(failed_brain)
        except ValueError:
            idx = -1

        for candidate in chain[idx + 1:]:
            if candidate in self._brains:
                return candidate

        return None

    async def get_all_info(self) -> List[Dict[str, Any]]:
        """Get info from all brains."""
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
