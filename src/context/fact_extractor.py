"""Fact Extractor — lightweight learning loop for AURA.

After each brain response, runs a tiny OpenRouter call (fast free model)
to extract facts worth remembering. Runs in the background so it never
delays the response to Ricardo.

Extracted facts are saved to ~/.aura/brain/memory.md automatically.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import structlog

from .aura_context import update_memory, add_client, add_task, _SECTION_CLIENTS, _SECTION_TASKS, _SECTION_NOTES

logger = structlog.get_logger()

_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_EXTRACTOR_MODEL = "meta-llama/llama-3.2-3b-instruct:free"  # fast + cheap
_EXTRACTOR_TIMEOUT = 15  # never block for more than 15s

_EXTRACT_SYSTEM = """\
You are a fact extractor for an AI assistant's memory system.
Given a conversation (user message + assistant response), extract 0-3 facts worth remembering.

Facts worth remembering:
- Client info: emails, names, companies, relationships
- Task outcomes: what was done, what worked, what failed
- Preferences: how Ricardo likes things done
- Project state: new projects, status changes
- Configuration: keys, paths, services discovered

Do NOT extract:
- General knowledge or common facts
- Temporary state or current session context
- Things already obvious from context

Reply ONLY with a JSON array of strings. Example:
["hola@idnt.es es nuevo cliente de RUD", "dominio royaluniondesign.com pendiente verificar en Resend"]

If nothing worth remembering, reply: []
"""


def _get_openrouter_key() -> Optional[str]:
    """Get OpenRouter key from env or opencode auth file."""
    import os
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        auth = Path.home() / ".local/share/opencode/auth.json"
        if auth.exists():
            try:
                data = json.loads(auth.read_text())
                key = data.get("openrouter", {}).get("key")
            except Exception:
                pass
    return key


def extract_facts(user_message: str, assistant_response: str) -> list[str]:
    """Call a tiny LLM to extract facts from an interaction.

    Returns list of fact strings (may be empty).
    Raises no exceptions — always returns safely.
    """
    key = _get_openrouter_key()
    if not key:
        return []

    conversation = (
        f"User: {user_message[:500]}\n\n"
        f"Assistant: {assistant_response[:1000]}"
    )

    try:
        body = json.dumps({
            "model": _EXTRACTOR_MODEL,
            "messages": [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": conversation},
            ],
            "max_tokens": 256,
            "temperature": 0.1,  # deterministic fact extraction
        }).encode()

        req = urllib.request.Request(
            _API_URL, data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://aura.local",
                "X-Title": "AURA-FactExtractor",
            },
        )
        with urllib.request.urlopen(req, timeout=_EXTRACTOR_TIMEOUT) as resp:
            data = json.loads(resp.read())

        raw = (data["choices"][0]["message"]["content"] or "").strip()

        # Parse JSON array
        if raw.startswith("["):
            facts = json.loads(raw)
            if isinstance(facts, list):
                return [str(f) for f in facts if f]
    except json.JSONDecodeError:
        pass  # model didn't return valid JSON — ignore
    except Exception as e:
        logger.debug("fact_extractor_error", error=str(e))

    return []


def _classify_and_save(fact: str) -> None:
    """Classify a fact and save to appropriate memory section."""
    fact_lower = fact.lower()

    # Client fact: contains email pattern
    import re
    if re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", fact):
        add_client(
            email=re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", fact).group(),  # type: ignore[union-attr]
            notes=fact,
        )
        return

    # Task fact: past tense action words
    task_words = ["enviado", "creado", "instalado", "configurado", "arreglado",
                  "sent", "created", "fixed", "installed", "configured", "completed"]
    if any(w in fact_lower for w in task_words):
        add_task(fact)
        return

    # Everything else → notes
    update_memory(fact, _SECTION_NOTES)


def learn_from_interaction(user_message: str, assistant_response: str) -> int:
    """Extract and save facts from an interaction. Returns count saved.

    Designed to be called in background (asyncio.ensure_future).
    """
    # Skip trivial interactions
    if len(assistant_response) < 80:
        return 0
    # Skip error messages
    if assistant_response.startswith("❌") or assistant_response.startswith("⏱"):
        return 0

    try:
        facts = extract_facts(user_message, assistant_response)
        for fact in facts:
            _classify_and_save(fact)
        if facts:
            logger.info("facts_learned", count=len(facts), facts=facts)
        return len(facts)
    except Exception as e:
        logger.debug("learn_error", error=str(e))
        return 0
