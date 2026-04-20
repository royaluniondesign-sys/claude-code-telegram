"""Brain Router — smart routing with free-tier cascade and pre-flight rate check.

Routing philosophy:
  Maximize free tier usage before touching Claude subscription.
  Claude (haiku/sonnet/opus) = reliable baseline, used when free tiers fail or
  the task is complex enough to warrant it (meta-router complexity score).

Full cascade (cost-ascending, free → paid):
  1. api-zero    — weather/crypto/currency/QR/dict — instant, zero LLM
  2. ollama-rud  — remote LAN Ollama — free, unlimited, code-focused
  3. qwen-code   — Alibaba Qwen Code CLI — free 1000 req/day, code+general
  4. opencode    — OpenCode CLI (OpenRouter backend) — free, code gen
  5. gemini      — Google CLI — free, web-aware, search/URL analysis
  6. openrouter  — HTTP free cascade — free public models (needs API key)
  7. cline       — local Ollama — $0 if Ollama running locally
  8. codex       — ChatGPT subscription
  9. haiku       — Claude CLI — fast, reliable, subscription
  10. sonnet     — Claude CLI — balanced complexity
  11. opus       — Claude CLI — deepest reasoning, architecture

Fallback chain: each free brain cascades to the next free brain before hitting Claude.
  ollama-rud → qwen-code → opencode → gemini → openrouter → cline → haiku → sonnet → opus

Adding a new CLI brain (template):
  1. Create src/brains/<name>_brain.py — implement Brain ABC (execute, health_check, get_info)
  2. Import here, add to BrainRouter.__init__()
  3. Add to _FULL_CASCADE at the right cost/speed position
  4. Add to _FREE_FALLBACK: brain → next_on_failure
  5. Optionally update _INTENT_BRAIN_MAP for direct routing

Complexity gating (meta-router):
  score < 5  → free tier (gemini/openrouter/cline)
  score 5-14 → haiku (Claude, moderate complexity)
  score ≥ 15 → sonnet (Claude, high complexity)

Pre-flight: if selected brain is rate-limited, walk cascade until available brain found.
Full cascade: when a brain fails, try every next in chain until one succeeds.
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


# Full cascade chain — failover order (free → paid)
# Every brain that fails cascades to the next one in this list.
# Initial routing uses _INTENT_BRAIN_MAP; this is purely for failover.
_FULL_CASCADE: List[str] = [
    "api-zero",  # Instant public APIs — no LLM, 0ms
    "haiku",     # Claude Haiku — fast, cheap, subscription
    "codex",     # Codex CLI — ChatGPT Team subscription, gpt-5.4, code-focused
    "gemini",    # Google Gemini CLI — web-aware (search only)
    "sonnet",    # Claude Sonnet — balanced, deep reasoning
    "opus",      # Claude Opus — deepest reasoning, architecture
]

# Intent → primary brain.
# Principle: Claude subscription first (haiku/sonnet), Codex for code,
# Gemini only for real web search. Zero external LLM CLIs for general tasks.
_INTENT_BRAIN_MAP: Dict[Intent, str] = {
    Intent.BASH: "zero-token",    # shell/git/files → instant, 0 tokens
    Intent.FILES: "zero-token",
    Intent.GIT: "zero-token",
    Intent.SEARCH: "gemini",      # web search → gemini (real web access)
    Intent.TRANSLATE: "haiku",    # translation → Claude Haiku
    Intent.CHAT: "haiku",         # chat → Claude Haiku (fast, subscription)
    Intent.DEEP: "sonnet",        # deep analysis → Claude Sonnet
    Intent.CODE: "codex",         # code → Codex (ChatGPT Team, gpt-5.4, $0 extra)
    Intent.EMAIL: "haiku",        # email → Claude (Resend tool access)
    Intent.CALENDAR: "haiku",     # calendar → Claude (calendar tool access)
    Intent.IMAGE: "image",        # image → pollinations.ai FLUX.1 (free, 0 tokens)
}

# API-zero keyword shortcuts — checked before intent routing
# Any match → api-zero wins (instant, no LLM, no tokens)
_API_ZERO_PATTERNS: List[str] = [
    r"(?i)\b(clima|tiempo|weather|temperatura|llueve|calor|fr[íi]o)\b",
    r"(?i)\b(bitcoin|btc|eth|ethereum|crypto|criptomoneda)\b",
    r"(?i)\b(cambio|convertir|tipo\s+de\s+cambio|exchange\s+rate)\b",
    r"(?i)\b(qr|c[oó]digo\s+qr|genera\s+qr)\b",
    r"(?i)\b(qu[eé]\s+significa|define|meaning\s+of|definici[oó]n\s+de)\b",
]

# Per-brain failover: brain → next brain when it fails/rate-limits.
# Simple 4-step chain: haiku → codex → sonnet → opus
# Gemini is search-only, no failover destination (has its own fallback to haiku).
_FREE_FALLBACK: Dict[str, str] = {
    "haiku": "codex",    # Haiku rate-limited → Codex (ChatGPT Team)
    "codex": "sonnet",   # Codex fails/rate-limited → Sonnet (Claude)
    "gemini": "haiku",   # Gemini search fails → Haiku
    "sonnet": "opus",    # Sonnet fails → Opus
    "opus": "sonnet",    # Opus fails → Sonnet (loop guard)
    "cline": "haiku",    # Cline (local Ollama) offline → Haiku
}

# Free brains escalate to Claude only at OPUS-level (score ≥ 20, set in meta_router.py).
# Claude tiers (haiku/sonnet/opus) are selected among themselves once Claude is the target.


class BrainRouter:
    """Manages all brains with smart routing and rate-limit-aware cascading."""

    def __init__(self) -> None:
        self._brains: Dict[str, Brain] = {}
        self._active_brain: str = "haiku"
        # Per-user brain override (user_id -> brain_name)
        self._user_brains: Dict[int, str] = {}

        # Zero-cost instant API brain (weather, crypto, currency, QR, dictionary)
        self._brains["api-zero"] = ApiBrain(timeout=8)

        # Claude — primary brains (subscription, no API key, always available)
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
        """Classify message intent and route to optimal brain.

        With rate_monitor: skips rate-limited brains and cascades to fallback
        before even sending the request.

        urgent=True forces Haiku (min latency) and skips free-tier brains.

        Returns (brain_name, intent_result).
        """
        import re as _re
        intent = classify_semantic(message)

        # Auto-detect urgency from message text if not already set
        if not urgent:
            _urgent_pat = _re.compile(
                r"(?i)\b(urgente|urgent|asap|ahora\s+mismo|inmediato|r[áa]pido|"
                r"rapido|ya\s+mismo|right\s+now|immediately|emergency|critico|cr[íi]tico)\b"
                r"|!!+"
            )
            if _urgent_pat.search(message):
                urgent = True
                logger.debug("smart_route_urgent_detected", snippet=message[:60])

        # Zero-token always wins — bash, git, file commands (no LLM)
        if intent.intent in (Intent.BASH, Intent.FILES, Intent.GIT):
            return "zero-token", intent

        # API-zero: instant free APIs for weather/crypto/currency/QR/dictionary
        if "api-zero" in self._brains and not urgent:
            for _pat in _API_ZERO_PATTERNS:
                if _re.search(_pat, message):
                    logger.debug("smart_route_api_zero", pattern=_pat[:40])
                    return "api-zero", intent

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

        # ── Complexity gate: meta-router ONLY selects between Claude tiers ──
        #
        # RULE: Free brains (qwen-code, gemini, ollama-rud, opencode, cline) are
        # NEVER bypassed by meta-router. They always get first shot.
        # meta-router only runs when the target is already a Claude model,
        # to decide which tier (haiku/sonnet/opus) to use.
        #
        # Claude is invoked when:
        #   1. Intent requires Claude tools (EMAIL, CALENDAR → haiku directly)
        #   2. Free brain cascade exhausted after failures
        #   3. Urgent flag set (latency critical)
        #   4. User explicitly locked brain via /brain
        try:
            from ..claude.meta_router import route_request as _meta_route, ModelTier
            decision = _meta_route(message, urgent=urgent)

            if urgent:
                # Urgent → skip everything, go to haiku (fastest Claude)
                if target not in ("haiku", "sonnet", "opus"):
                    logger.info("smart_route_urgent", from_brain=target, to="haiku")
                    target = "haiku"

            elif target in ("haiku", "sonnet", "opus"):
                # Already routing to Claude → pick the right tier
                if decision.tier == ModelTier.OPUS and target == "haiku":
                    target = "sonnet"   # haiku → sonnet (opus reached via cascade)
                    logger.info("meta_router_upgrade_sonnet", score=decision.score)
                elif decision.tier == ModelTier.SONNET and target == "haiku":
                    target = "sonnet"
                    logger.info("meta_router_upgrade_sonnet", score=decision.score)
                # else: keep target (score low → haiku fine, or already sonnet/opus)

            else:
                # Free brain selected — only escalate on OPUS-level complexity (score ≥ 20).
                # SONNET-level (10-19): free brains handle it, cascade naturally if needed.
                # HAIKU-level (<10): always free brain.
                if decision.tier == ModelTier.OPUS:
                    logger.info(
                        "meta_router_opus_escalate",
                        from_brain=target, score=decision.score, to="sonnet",
                    )
                    target = "sonnet"  # Very hard problem → Claude Sonnet minimum

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
