"""AURA Meta-Router — complexity detection + model escalation.

Analyses incoming messages/tasks and decides which model tier to use:
  - Haiku   → fast, cheap: simple questions, shell ops, quick lookups
  - Sonnet  → balanced: code review, moderate analysis, multi-step tasks
  - Opus    → deep: architecture decisions, large refactors, complex debugging

The router uses a scoring system based on:
1. Message length
2. Complexity keywords (weighted by language: ES + EN)
3. Task category / context signals
4. Urgency flag (urgent → fast → Haiku regardless)

Usage:
    from src.claude.meta_router import route_request, ModelTier

    tier = route_request(text="refactoriza el módulo de auth completo", urgent=False)
    # → ModelTier.SONNET
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()


# ── Model Tiers ─────────────────────────────────────────────────────────────

class ModelTier(str, Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"

    @property
    def label(self) -> str:
        return {
            ModelTier.HAIKU: "Claude Haiku (fast)",
            ModelTier.SONNET: "Claude Sonnet (balanced)",
            ModelTier.OPUS: "Claude Opus (deep)",
        }[self]


# ── Scoring Weights ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _KeywordGroup:
    tier: ModelTier
    weight: int
    patterns: list[str]


_GROUPS: list[_KeywordGroup] = [
    # Opus-level: deep reasoning required
    _KeywordGroup(
        tier=ModelTier.OPUS,
        weight=10,
        patterns=[
            r"\barquitectura\b", r"\barchitecture\b",
            r"\bsistema distribuido\b", r"\bdistributed system\b",
            r"\brefactor\s+(completo|total|masivo|grande|toda)\b",
            r"\bfull\s+refactor\b",
            r"\bpor\s+qu[eé]\s+(fall[oó]|no\s+funciona|rompe)\b",
            r"\bwhy\s+(is\s+it\s+(failing|broken)|doesn.t\s+work)\b",
            r"\bdise[ñn]a?\b.*\bsistema\b",
            r"\bdesign\b.*\bsystem\b",
            r"\btradeoff\b", r"\btrade.off\b",
            r"\bscalabilit\w+\b", r"\bescalabilidad\b",
            r"\bconcurrencia\b", r"\bconcurrency\b",
            r"\brace\s+condition\b",
        ],
    ),
    # Sonnet-level: code + analysis
    _KeywordGroup(
        tier=ModelTier.SONNET,
        weight=5,
        patterns=[
            r"\brefactor\b",
            r"\bdebug\b", r"\bdebugging\b",
            r"\banaliz[ae]\b", r"\banaly[sz]e\b",
            r"\brevisar?\b.*\bc[oó]digo\b", r"\bcode\s+review\b",
            r"\boptimiz[ae]\b", r"\boptimize?\b",
            r"\btest\b.*\bescribir\b|\bescribir\b.*\btest\b",
            r"\bwrite\s+tests?\b",
            r"\bexplica\s+(c[oó]mo|por\s+qu[eé])\b",
            r"\bexplain\s+(how|why)\b",
            r"\bimplementa\b", r"\bimplemented?\b",
            r"\bmigra[cr]\b", r"\bmigrat\b",
            r"\bintegra[cr]\b", r"\bintegrat\b",
            r"\bapi\b.*\bdesign\b|\bdesign\b.*\bapi\b",
            r"\bbase\s+de\s+datos\b", r"\bdatabase\s+schema\b",
            r"\bsql\b", r"\bquery\b",
            r"\bsegurid\w+\b", r"\bsecurity\b",
            r"\bvulnerabilidad\b", r"\bvulnerabilit\w+\b",
            r"\berror\s+handling\b", r"\bmanejo\s+de\s+errores\b",
            r"\bpull\s+request\b", r"\bpr\s+review\b",
        ],
    ),
    # Haiku-level: fast ops
    _KeywordGroup(
        tier=ModelTier.HAIKU,
        weight=-3,  # Negative = pushes DOWN toward Haiku
        patterns=[
            r"^[!/]",             # Commands starting with ! or /
            r"\bls\b|\bpwd\b|\bgit\s+status\b",
            r"\bqu[eé]\s+hora\b", r"\bwhat\s+time\b",
            r"\bresumen?\s+r[aá]pido\b", r"\bquick\s+summar\b",
            r"\btraduc[ei]\b", r"\btranslat\b",
            r"\bdef\b.{0,40}\bdef\b",  # Short code snippet
        ],
    ),
]

# Base thresholds
_SONNET_THRESHOLD = 5
_OPUS_THRESHOLD = 15

# Length scoring: long messages signal complexity
_LENGTH_THRESHOLDS = [
    (200, 2),   # > 200 chars → +2
    (500, 5),   # > 500 chars → +5
    (1000, 8),  # > 1000 chars → +8
]

# ── Router ───────────────────────────────────────────────────────────────────

@dataclass
class RouteDecision:
    tier: ModelTier
    score: int
    signals: list[str] = field(default_factory=list)
    urgent: bool = False


def route_request(
    text: str,
    *,
    urgent: bool = False,
    category: Optional[str] = None,
    context_tokens: int = 0,
) -> RouteDecision:
    """Decide which model tier to use for this request.

    Args:
        text: The user message or task description.
        urgent: If True, forces Haiku for minimal latency.
        category: Task category hint ("fix", "analysis", "architecture", etc.)
        context_tokens: Estimated token count of context (large → escalate)

    Returns:
        RouteDecision with tier, score, and signals list.
    """
    signals: list[str] = []
    score = 0

    # Urgency short-circuit
    if urgent:
        logger.debug("meta_router_urgent", tier=ModelTier.HAIKU)
        return RouteDecision(
            tier=ModelTier.HAIKU,
            score=0,
            signals=["urgent flag → Haiku (min latency)"],
            urgent=True,
        )

    lower = text.lower()

    # 1. Length scoring
    for threshold, pts in _LENGTH_THRESHOLDS:
        if len(text) > threshold:
            score += pts
            signals.append(f"length>{threshold} (+{pts})")

    # 2. Keyword scoring
    for group in _GROUPS:
        matched = []
        for pat in group.patterns:
            if re.search(pat, lower):
                matched.append(pat)
        if matched:
            contribution = group.weight * len(matched)
            score += contribution
            short_pats = [p[:30] for p in matched[:3]]
            signals.append(f"{group.tier.value} keywords {short_pats} ({contribution:+d})")

    # 3. Category hint
    if category in ("architecture", "analysis"):
        score += 8
        signals.append(f"category={category} (+8)")
    elif category in ("fix", "optimize"):
        score += 4
        signals.append(f"category={category} (+4)")
    elif category in ("maintenance",):
        score -= 2
        signals.append(f"category={category} (-2)")

    # 4. Context token pressure
    if context_tokens > 50_000:
        score += 5
        signals.append(f"context_tokens={context_tokens} (+5)")

    # 5. Decide tier
    if score >= _OPUS_THRESHOLD:
        tier = ModelTier.OPUS
    elif score >= _SONNET_THRESHOLD:
        tier = ModelTier.SONNET
    else:
        tier = ModelTier.HAIKU

    logger.debug(
        "meta_router_decision",
        tier=tier,
        score=score,
        signals=signals[:5],
    )

    return RouteDecision(tier=tier, score=score, signals=signals)


def explain_decision(decision: RouteDecision) -> str:
    """Return a human-readable explanation of the routing decision."""
    lines = [
        f"🧭 *Meta-router* → {decision.tier.label}",
        f"Score: {decision.score} (Sonnet≥{_SONNET_THRESHOLD}, Opus≥{_OPUS_THRESHOLD})",
    ]
    if decision.signals:
        lines.append("Signals:")
        for sig in decision.signals[:5]:
            lines.append(f"  • {sig}")
    return "\n".join(lines)


# ── Quick helpers ────────────────────────────────────────────────────────────

def should_escalate(text: str, current_tier: ModelTier = ModelTier.HAIKU) -> bool:
    """True if the text warrants a higher tier than current."""
    decision = route_request(text)
    tier_order = [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS]
    return tier_order.index(decision.tier) > tier_order.index(current_tier)


def is_complex(text: str) -> bool:
    """Quick check: does this text need Sonnet or above?"""
    return route_request(text).tier in (ModelTier.SONNET, ModelTier.OPUS)
