"""Brain Router — simple routing: OpenRouter free → haiku fallback.

Routing philosophy (simplified — mirrors Hermes):
  1. zero-token  — shell/git/files → instant, no LLM
  2. image       — image generation → pollinations.ai
  3. haiku       — email/calendar → Claude (needs tool access)
  4. openrouter  — EVERYTHING ELSE → free model cascade
     └─ fallback → haiku (Claude subscription)

No complex intent routing for general tasks. No Codex, no Gemini in main flow.
Gemini only for explicit web search. Claude only as reliable fallback.
"""

from typing import Any, Dict, List, Optional

import structlog

from .api_brain import ApiBrain
from .autonomous_brain import AutonomousBrain
from .base import Brain, BrainResponse, BrainStatus
from .claude_brain import ClaudeBrain
from .executor_brain import ClineBrain, CodexBrain
from .gemini_brain import GeminiBrain
from .image_brain import ImageBrain
from .ollama_brain import OllamaBrain
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


# Cascade order — used for fallback resolution
_FULL_CASCADE: List[str] = [
    "api-zero",    # instant public APIs
    "openrouter",  # free models primary
    "haiku",       # Claude fallback
    "sonnet",      # Claude deeper
    "opus",        # Claude deepest
]

# Intent → primary brain
# BASH/FILES/GIT → zero-token (instant, no LLM)
# IMAGE → image brain (pollinations)
# EMAIL/CALENDAR → haiku (needs Claude tool access)
# CHAT/CODE/TRANSLATE → haiku (direct user interactions — quality + reliability)
# DEEP → sonnet (complex reasoning deserves it)
# SEARCH → openrouter (bulk lookups, quality less critical)
# Pressure override in smart_route: if haiku > 70% → non-tool intents → openrouter
_INTENT_BRAIN_MAP: Dict[Intent, str] = {
    Intent.BASH: "zero-token",
    Intent.FILES: "zero-token",
    Intent.GIT: "zero-token",
    Intent.IMAGE: "image",
    Intent.EMAIL: "haiku",       # needs Claude tool access — never downgrade
    Intent.CALENDAR: "haiku",    # needs Claude tool access — never downgrade
    Intent.CHAT: "openrouter",   # conversation — OpenRouter ~1-2s vs Haiku CLI ~8-12s
    Intent.SEARCH: "openrouter", # lookups — speed beats marginal quality gain
    Intent.TRANSLATE: "openrouter", # translation — free models match Haiku here
    Intent.CODE: "haiku",        # code — correctness + safety require Claude
    Intent.DEEP: "sonnet",       # complex analysis — Sonnet earns its place
}

# API-zero keyword shortcuts — checked before intent routing
_API_ZERO_PATTERNS: List[str] = [
    r"(?i)\b(clima|tiempo|weather|temperatura|llueve|calor|fr[íi]o)\b",
    r"(?i)\b(bitcoin|btc|eth|ethereum|crypto|criptomoneda)\b",
    r"(?i)\b(cambio|convertir|tipo\s+de\s+cambio|exchange\s+rate)\b",
    r"(?i)\b(qr|c[oó]digo\s+qr|genera\s+qr)\b",
    r"(?i)\b(qu[eé]\s+significa|define|meaning\s+of|definici[oó]n\s+de)\b",
]

# Per-brain failover
_FREE_FALLBACK: Dict[str, str] = {
    "openrouter": "haiku",
    "haiku": "sonnet",
    "gemini": "haiku",
    "sonnet": "opus",
    "opus": "haiku",
    "cline": "haiku",
    "codex": "haiku",
}


class BrainRouter:
    """Manages all brains with smart routing and rate-limit-aware cascading."""

    def __init__(self) -> None:
        self._brains: Dict[str, Brain] = {}
        self._active_brain: str = "haiku"
        # Per-user brain override (user_id -> brain_name)
        self._user_brains: Dict[int, str] = {}

        # Zero-cost instant API brain (weather, crypto, currency, QR, dictionary)
        self._brains["api-zero"] = ApiBrain(timeout=8)

        # OpenRouter — free model cascade (primary for chat, no subscription cost)
        self._brains["openrouter"] = OpenRouterBrain(timeout=60)

        # Claude — subscription brains (fallback when free tiers fail or tools needed)
        self._brains["haiku"] = ClaudeBrain(model="haiku", timeout=60)
        self._brains["sonnet"] = ClaudeBrain(model="sonnet", timeout=180)
        self._brains["opus"] = ClaudeBrain(model="opus", timeout=300)

        # Codex — ChatGPT Team subscription (gpt-5.4, no API billing, code-focused)
        self._brains["codex"] = CodexBrain(timeout=90)

        # Gemini — Google CLI, web search only (free, has real internet access)
        self._brains["gemini"] = GeminiBrain(timeout=30)

        # Image gen — pollinations.ai FLUX.1 (free, 0 tokens)
        self._brains["image"] = ImageBrain(timeout=60)

        # Cline — local Ollama (optional, $0, code edits only, only if Ollama running)
        self._brains["cline"] = ClineBrain(timeout=60)

        # local-ollama — direct Ollama HTTP API (used by proactive loop L1/L2 steps)
        self._brains["local-ollama"] = OllamaBrain(timeout=120)

        # Autonomous — Claude Sonnet + full AURA MCP tools (conductor/proactive loop only)
        self._brains["autonomous"] = AutonomousBrain(timeout=300)

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
        urgent: bool = False,
    ) -> tuple:
        """Route to optimal brain based on intent + pressure awareness.

        Priority:
          zero-token > api-zero > user-lock > urgent > intent-map > pressure-downgrade
        Claude brains (haiku/sonnet) are primary for user interactions.
        OpenRouter handles bulk/background when Claude is under pressure.
        """
        import re as _re
        intent = classify_semantic(message)

        # Zero-token always wins
        if intent.intent in (Intent.BASH, Intent.FILES, Intent.GIT):
            return "zero-token", intent

        # API-zero: instant free APIs
        if "api-zero" in self._brains:
            for _pat in _API_ZERO_PATTERNS:
                if _re.search(_pat, message):
                    return "api-zero", intent

        # User explicit lock via /brain X takes priority
        if user_id is not None and user_id in self._user_brains:
            locked = self._user_brains[user_id]
            return locked, intent

        # Urgent → haiku directly (skip free tier)
        if urgent:
            return "haiku", intent

        # Map intent to target brain
        target = _INTENT_BRAIN_MAP.get(intent.intent, "haiku")

        if target == "zero-token":
            return "zero-token", intent

        # Pressure-aware downgrade: if target Claude brain is at 70%+,
        # shift non-tool intents to OpenRouter to preserve Claude capacity.
        # EMAIL and CALENDAR always stay on haiku (need tool access).
        _tool_intents = {Intent.EMAIL, Intent.CALENDAR}
        if (
            rate_monitor
            and target in ("haiku", "sonnet")
            and intent.intent not in _tool_intents
        ):
            try:
                usage = rate_monitor.get_usage(target)
                pct = usage.usage_pct
                if pct is not None and pct >= 0.70:
                    logger.info(
                        "claude_pressure_downgrade",
                        brain=target,
                        pct=round(pct, 2),
                        fallback="openrouter",
                    )
                    target = "openrouter"
            except Exception:
                pass

        # Skip hard rate-limited brain
        if rate_monitor and target in self._brains:
            try:
                if rate_monitor.get_usage(target).is_rate_limited:
                    target = _FREE_FALLBACK.get(target, "haiku")
                    logger.info("preflight_skip_ratelimited", fallback=target)
            except Exception:
                pass

        if target in self._brains:
            logger.info(
                "smart_route_decision",
                routed=target,
                intent=intent.intent.value,
            )
            return target, intent

        return "haiku", intent

    def get_cascade_chain(self, failed_brain: str) -> List[str]:
        """Return ordered list of brains to try after failed_brain.

        Normalizes "claude-haiku" → "haiku" etc. so ClaudeBrain.name
        (which prefixes "claude-") resolves correctly in the cascade.
        """
        normalized = (
            failed_brain[len("claude-"):]
            if failed_brain.startswith("claude-")
            else failed_brain
        )
        try:
            idx = _FULL_CASCADE.index(normalized)
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
