"""Semantic Router — embedding-based intent classification.

Replaces regex classify() with vector similarity search using fastembed.
Runs fully locally — no API cost, no internet needed for classification.
Falls back to regex classify() on init error or slow first-run download.

Model is loaded lazily on first call. First call triggers fastembed model
download (~50MB). Subsequent calls are fast (~5ms).
"""

from __future__ import annotations

import logging
from typing import Optional

import structlog

from .intent import Intent, IntentResult, classify as regex_classify

logger = structlog.get_logger()

# ── Route definitions — Spanish + English utterances per intent ────────────

_ROUTE_UTTERANCES: dict[str, list[str]] = {
    "bash": [
        "ejecuta este comando", "run this command", "corre este script",
        "ejecuta bash", "run bash script", "shell command",
        "ejecuta en terminal", "run in terminal", "execute this",
        "correr el script", "ejecutar", "run the following",
        "abre una terminal", "open terminal session",
    ],
    "files": [
        "lista los archivos", "list files", "muéstrame los archivos",
        "qué hay en esta carpeta", "what's in this folder",
        "lee el archivo", "read the file", "muestra el contenido",
        "show file contents", "cat the file", "abre el archivo",
        "crea un archivo", "create a file", "escribe en el archivo",
        "write to file", "borra el archivo", "delete file",
        "copia el archivo", "copy file", "mueve el archivo",
    ],
    "git": [
        "git status", "git log", "git diff", "git commit",
        "muéstrame los commits", "show git history",
        "haz un commit", "make a commit", "push los cambios",
        "push the changes", "crea una rama", "create branch",
        "fusiona la rama", "merge branch", "git pull",
        "revisa el repositorio", "check repo status",
        "qué cambié", "what did I change",
    ],
    "search": [
        "busca en internet", "search the web", "googlea",
        "qué es", "what is", "quién es", "who is",
        "investiga sobre", "research about", "find information about",
        "busca información", "look up", "dime sobre",
        "precio de", "price of", "cuánto cuesta", "how much does",
        "noticias de", "news about", "latest about",
        "busca online", "find online", "search for",
    ],
    "translate": [
        "traduce esto", "translate this", "en inglés", "in English",
        "en español", "in Spanish", "cómo se dice", "how do you say",
        "tradúceme", "translate for me", "to French", "al francés",
        "to German", "al alemán", "traducción de",
        "what does this mean in", "qué significa en",
    ],
    "code": [
        "escribe una función", "write a function", "crea un script",
        "create a script", "genera código", "generate code",
        "implementa", "implement", "crea una clase", "create a class",
        "refactoriza", "refactor this", "arregla el bug", "fix the bug",
        "debug esto", "debug this", "optimiza el código", "optimize code",
        "escribe un test", "write a test", "crea un endpoint",
        "create an API", "escribe el componente", "build component",
        "programa esto", "code this", "desarrolla", "develop",
    ],
    "chat": [
        "hola", "hello", "hey", "buenos días", "buenas tardes",
        "qué tal", "how are you", "gracias", "thank you", "thanks",
        "ok vale", "entendido", "understood", "perfecto", "perfect",
        "de acuerdo", "agreed", "cuéntame", "tell me",
        "qué opinas", "what do you think", "ayúdame", "help me",
        "explícame", "explain to me", "háblame de", "talk to me about",
    ],
    "email": [
        "envía un email", "send an email", "manda un correo",
        "escribe un email", "write an email", "redacta un correo",
        "draft an email", "responde al email", "reply to email",
        "revisa mi bandeja", "check my inbox", "lee mis emails",
        "reenvía el correo", "forward the email",
        "asunto del correo", "email subject", "to: ", "para: ",
    ],
    "calendar": [
        "agenda una reunión", "schedule a meeting", "crea un evento",
        "create an event", "añade al calendario", "add to calendar",
        "bloquea tiempo", "block time", "cuándo tengo reunión",
        "when is my meeting", "mis eventos de hoy", "today's events",
        "próxima reunión", "next meeting", "programa una cita",
        "schedule appointment", "recuérdame", "remind me",
        "recordatorio", "reminder", "disponibilidad", "availability",
    ],
    "deep": [
        "analiza esto en detalle", "analyze this in detail",
        "explícame a fondo", "explain in depth", "razona sobre",
        "reason about", "evalúa las opciones", "evaluate options",
        "compara estas alternativas", "compare these alternatives",
        "qué es mejor", "which is better", "pros y contras",
        "pros and cons", "arquitectura de", "architecture of",
        "diseño del sistema", "system design", "planifica",
        "plan this out", "revisa en profundidad", "review in depth",
        "resumen detallado", "detailed summary",
    ],
}

# Map intent name → suggested brain (mirrors router._INTENT_BRAIN_MAP)
_INTENT_BRAIN_MAP: dict[str, str] = {
    "bash": "zero-token",
    "files": "zero-token",
    "git": "zero-token",
    "search": "gemini",
    "translate": "openrouter",
    "chat": "openrouter",
    "code": "openrouter",
    "email": "haiku",
    "calendar": "haiku",
    "deep": "openrouter",
}

_INTENT_ENUM_MAP: dict[str, Intent] = {
    "bash": Intent.BASH,
    "files": Intent.FILES,
    "git": Intent.GIT,
    "search": Intent.SEARCH,
    "translate": Intent.TRANSLATE,
    "chat": Intent.CHAT,
    "code": Intent.CODE,
    "email": Intent.EMAIL,
    "calendar": Intent.CALENDAR,
    "deep": Intent.DEEP,
}

# Singleton router (lazy init)
_router: Optional[object] = None
_router_ready = False


def _build_router() -> object:
    """Build and return a SemanticRouter with fastembed encoder."""
    from semantic_router import Route
    from semantic_router.encoders import FastEmbedEncoder
    from semantic_router.routers import SemanticRouter

    encoder = FastEmbedEncoder(
        name="BAAI/bge-small-en-v1.5",
        score_threshold=0.4,
    )

    routes = [
        Route(name=name, utterances=utterances)
        for name, utterances in _ROUTE_UTTERANCES.items()
    ]

    router = SemanticRouter(encoder=encoder, routes=routes, top_k=1)
    return router


def _get_router() -> Optional[object]:
    """Return initialized router, or None if not ready."""
    global _router, _router_ready
    if _router_ready:
        return _router
    return None


def _init_router_sync() -> None:
    """Initialize router synchronously (call once at startup)."""
    global _router, _router_ready
    try:
        _router = _build_router()
        # Warm-up call: forces index build so first real message doesn't hit "Index is not ready"
        try:
            _router("hello")
        except Exception:
            pass
        _router_ready = True
        logger.info("semantic_router_ready")
    except Exception as e:
        logger.warning("semantic_router_init_failed", error=str(e))
        _router = None
        _router_ready = True  # mark ready so we stop retrying on every call


async def ensure_router_initialized() -> None:
    """Async wrapper — call once at bot startup to pre-warm embeddings."""
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_router_sync)


def classify_semantic(message: str) -> IntentResult:
    """Classify intent — regex only, fastembed disabled to save ~300MB RAM.

    The fastembed/semantic-router model is accurate but costly in memory.
    Regex patterns cover all common intents well. Falls back to CHAT intent
    (→ qwen-code) for anything unrecognized, which is safe and free.
    """
    # Regex handles everything — semantic model never loads.
    # Semantic router disabled: saves ~300MB RAM. Re-enable by restoring the
    # fastembed routing logic that was here before the 2026-04-20 refactor.
    return regex_classify(message)
