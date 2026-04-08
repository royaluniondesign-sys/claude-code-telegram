"""Intent classifier — zero-LLM routing decisions.

Uses regex patterns to classify user messages and route
to the optimal brain (or zero-token handler).

Routing logic:
- Zero-token: shell, files, git (no LLM)
- Gemini: search, URLs, translate, email, calendar (needs internet)
- Ollama: code, chat, analysis (local, no internet needed)
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Intent(Enum):
    """Message intent categories."""

    BASH = "bash"           # Shell commands → zero-token
    FILES = "files"         # File operations → zero-token
    GIT = "git"             # Git operations → zero-token
    SEARCH = "search"       # Web search → Gemini (needs internet)
    TRANSLATE = "translate"  # Translation → Gemini (needs internet)
    CODE = "code"           # Coding tasks → Ollama (local)
    CHAT = "chat"           # General chat → Ollama (local)
    EMAIL = "email"         # Email → Gemini (needs internet)
    CALENDAR = "calendar"   # Calendar → Gemini (needs internet)
    DEEP = "deep"           # Deep analysis → Ollama (local)


@dataclass(frozen=True)
class IntentResult:
    """Classification result."""

    intent: Intent
    confidence: float  # 0.0-1.0
    suggested_brain: str  # "zero-token", "ollama", "gemini"
    reason: str


# Pattern groups — order matters (first match wins for high confidence)
_PATTERNS: list = [
    # Zero-token patterns (highest priority — save all tokens)
    (Intent.BASH, r"^[!$]", "zero-token", 1.0, "bash prefix"),
    (Intent.FILES, r"^/(?:ls|pwd|sh)\b", "zero-token", 1.0, "file command"),
    (Intent.GIT, r"^/git\b", "zero-token", 1.0, "git command"),

    # Explicit CLI routing — "usa opencode/cline/codex para X"
    (Intent.CODE, r"(?i)\busa\s+opencode\b", "opencode", 1.0, "explicit opencode"),
    (Intent.CODE, r"(?i)\busa\s+cline\b",    "cline",    1.0, "explicit cline"),
    (Intent.CODE, r"(?i)\busa\s+codex\b",    "codex",    1.0, "explicit codex"),
    (Intent.CODE, r"(?i)\buse\s+opencode\b", "opencode", 1.0, "explicit opencode"),
    (Intent.CODE, r"(?i)\buse\s+cline\b",    "cline",    1.0, "explicit cline"),
    (Intent.CODE, r"(?i)\buse\s+codex\b",    "codex",    1.0, "explicit codex"),

    # URL detection → Gemini (needs internet)
    (Intent.SEARCH, r"https?://", "gemini", 0.95, "URL detected"),

    # Email/Calendar → Gemini (needs internet access)
    (Intent.EMAIL, r"(?i)\b(?:email|correo|inbox|mail|enviar?\s+email|responde?\s+al?\s+email)\b", "gemini", 0.9, "email keyword"),
    (Intent.CALENDAR, r"(?i)\b(?:calendar|calendario|reunión|meeting|agenda|evento|cita)\b", "gemini", 0.9, "calendar keyword"),

    # Search/translate → Gemini (needs internet)
    (Intent.SEARCH, r"(?i)\b(?:busca|search|googl|find\s+info|investiga|qué\s+es|what\s+is|quién\s+es|who\s+is)\b", "gemini", 0.8, "search keyword"),
    (Intent.TRANSLATE, r"(?i)(?:\btraduc\w*|\btranslat\w*|\ben\s+inglés\b|\ben\s+español\b|\bto\s+english\b|\bto\s+spanish\b)", "gemini", 0.85, "translate keyword"),

    # Knowledge tasks Ollama handles locally (no internet needed)
    (Intent.DEEP, r"(?i)\b(?:resumen|resume|resumir|summarize|summary|cuéntame|dime\s+sobre)\b", "ollama", 0.75, "summarize/explain"),
    (Intent.DEEP, r"(?i)\b(?:define|definición|definition|significa|meaning|diferencia\s+entre|difference\s+between)\b", "ollama", 0.75, "definition/compare"),
    # Web lookups that DO need internet → Gemini
    (Intent.SEARCH, r"(?i)\b(?:tell\s+me\s+about|info\s+sobre|who\s+is)\b", "gemini", 0.75, "web info lookup"),
    (Intent.SEARCH, r"(?i)\b(?:precio|price|costo|cost|tarifa|rate|cuánto\s+cuesta|how\s+much)\b", "gemini", 0.8, "pricing keyword"),
    # Only web-visit verbs → Gemini (revisa/check/mira are local analysis, not web)
    (Intent.SEARCH, r"(?i)\b(?:visita|visit|abre\s+(?:la\s+)?(?:url|página|page|web|site|link))\b", "gemini", 0.7, "visit keyword"),

    # Recommendations/lists → Gemini (better with web knowledge)
    (Intent.SEARCH, r"(?i)\b(?:lista|list|enumera|nombre|recomienda|recommend|suggest|sugiere)\b", "gemini", 0.7, "list/recommend keyword"),

    # Code generation → Ollama (local)
    (Intent.CODE, r"(?i)\b(?:genera|generate|escribe?\s+(?:un|una|el|la)\s+(?:script|función|function|class|componente))\b", "ollama", 0.85, "generate code"),
    (Intent.CODE, r"(?i)\b(?:crea?\s+(?:un|una|el|la)\s+(?:función|class|componente|api|endpoint))\b", "ollama", 0.85, "create code"),

    # Code review/refactor/debug → Ollama (local)
    (Intent.CODE, r"(?i)\b(?:refactor|debug|fix|arregla|corrige|optimiza|mejora\s+el\s+código)\b", "ollama", 0.85, "fix/refactor"),
    (Intent.CODE, r"(?i)\b(?:test|unittest|pytest|jest|coverage)\b", "ollama", 0.8, "testing keyword"),
    (Intent.CODE, r"(?i)```", "ollama", 0.7, "code block"),

    # Deep analysis → Ollama (local reasoning)
    (Intent.DEEP, r"(?i)\b(?:analiz|explica|compara|diseña|arquitectura|planifica|review)\b", "ollama", 0.75, "analysis keyword"),

    # Simple greetings/acks → Ollama (fast local response)
    (Intent.CHAT, r"(?i)^(?:hola|hey|hi|hello|buenos?\s+días?|buenas)\b", "ollama", 0.6, "greeting"),
    (Intent.CHAT, r"(?i)\b(?:gracias|thanks|ok|vale|perfecto|genial)\b", "ollama", 0.6, "ack/thanks"),
]


def classify(message: str) -> IntentResult:
    """Classify a message intent using regex patterns.

    Returns the highest-confidence match, defaulting to CHAT→Ollama.
    """
    best: Optional[IntentResult] = None

    for intent, pattern, brain, confidence, reason in _PATTERNS:
        if re.search(pattern, message):
            result = IntentResult(
                intent=intent,
                confidence=confidence,
                suggested_brain=brain,
                reason=reason,
            )
            if best is None or result.confidence > best.confidence:
                best = result

    if best is not None:
        return best

    # Default: general chat → Ollama (local, free)
    return IntentResult(
        intent=Intent.CHAT,
        confidence=0.5,
        suggested_brain="ollama",
        reason="default",
    )
