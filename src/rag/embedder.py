"""Async Ollama embedder with in-process LRU cache.

Uses httpx (already installed) instead of aiohttp.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import List, Optional

import httpx
import numpy as np
import structlog

logger = structlog.get_logger()

OLLAMA_URL = "http://localhost:11434/api/embed"
OLLAMA_PULL_URL = "http://localhost:11434/api/pull"
MODEL = "nomic-embed-text"
_TIMEOUT = 10.0  # seconds
# nomic-embed-text supports ~8192 tokens; 2000 chars is a safe ceiling (~500 tokens)
_MAX_TEXT_CHARS = 2000

# Simple in-process cache: sha256(text) → numpy array
_embed_cache: dict[str, np.ndarray] = {}
_MAX_CACHE = 1000


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _cache_get(key: str) -> Optional[np.ndarray]:
    return _embed_cache.get(key)


def _cache_set(key: str, vec: np.ndarray) -> None:
    if len(_embed_cache) >= _MAX_CACHE:
        # Evict oldest entry (insertion order preserved in Python 3.7+)
        oldest = next(iter(_embed_cache))
        del _embed_cache[oldest]
    _embed_cache[key] = vec


async def embed(text: str) -> Optional[np.ndarray]:
    """Embed a single text. Returns None if Ollama unavailable."""
    key = _cache_key(text)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    results = await embed_batch([text])
    return results[0]


def _safe_truncate(text: str) -> str:
    """Truncate text to _MAX_TEXT_CHARS to avoid context-length errors."""
    return text[:_MAX_TEXT_CHARS] if len(text) > _MAX_TEXT_CHARS else text


async def _embed_single(client: httpx.AsyncClient, text: str) -> Optional[np.ndarray]:
    """Embed one text (with truncation), returns None on failure."""
    safe = _safe_truncate(text)
    key = _cache_key(safe)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        resp = await client.post(OLLAMA_URL, json={"model": MODEL, "input": [safe]})
        if resp.status_code != 200:
            logger.warning("ollama_single_embed_failed", status=resp.status_code)
            return None
        embs = resp.json().get("embeddings", [])
        if not embs:
            return None
        vec = np.array(embs[0], dtype=np.float32)
        _cache_set(key, vec)
        return vec
    except Exception as exc:
        logger.warning("ollama_single_embed_exception", error=str(exc))
        return None


async def embed_batch(texts: List[str]) -> List[Optional[np.ndarray]]:
    """Embed multiple texts in a single Ollama API call.

    Returns list of arrays (None where embedding failed).
    On 400 (context-length exceeded), falls back to per-text embedding with truncation.
    """
    if not texts:
        return []

    # Truncate all texts upfront to avoid context errors
    safe_texts = [_safe_truncate(t) for t in texts]

    # Check cache first
    results: List[Optional[np.ndarray]] = []
    uncached_indices: List[int] = []
    uncached_texts: List[str] = []

    for i, text in enumerate(safe_texts):
        key = _cache_key(text)
        cached = _cache_get(key)
        if cached is not None:
            results.append(cached)
        else:
            results.append(None)
            uncached_indices.append(i)
            uncached_texts.append(text)

    if not uncached_texts:
        return results

    payload = {"model": MODEL, "input": uncached_texts}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(OLLAMA_URL, json=payload)

            if resp.status_code == 404:
                # Model not loaded — try pulling and retry once
                logger.warning("ollama_model_not_found", model=MODEL)
                await _pull_model(client)
                resp = await client.post(OLLAMA_URL, json=payload)

            if resp.status_code == 400:
                # Batch too large or context exceeded — fall back to per-text
                logger.warning("ollama_batch_400_fallback_to_individual", count=len(uncached_texts))
                for orig_idx, text in zip(uncached_indices, uncached_texts):
                    vec = await _embed_single(client, text)
                    results[orig_idx] = vec
                return results

            if resp.status_code != 200:
                logger.warning("ollama_embed_error", status=resp.status_code, body=resp.text[:200])
                return results

            data = resp.json()

    except asyncio.TimeoutError:
        logger.warning("ollama_embed_timeout", url=OLLAMA_URL)
        return results
    except httpx.ConnectError:
        logger.warning("ollama_not_reachable", url=OLLAMA_URL)
        return results
    except Exception as exc:
        logger.error("ollama_embed_exception", error=str(exc))
        return results

    embeddings = data.get("embeddings", [])
    for idx, (original_idx, text) in enumerate(zip(uncached_indices, uncached_texts)):
        if idx < len(embeddings):
            vec = np.array(embeddings[idx], dtype=np.float32)
            key = _cache_key(text)
            _cache_set(key, vec)
            results[original_idx] = vec

    return results


async def _pull_model(client: httpx.AsyncClient) -> None:
    """Request Ollama to pull the embedding model (blocking up to 120s)."""
    try:
        resp = await client.post(
            OLLAMA_PULL_URL,
            json={"name": MODEL, "stream": False},
            timeout=120.0,
        )
        if resp.status_code == 200:
            logger.info("ollama_model_pulled", model=MODEL)
        else:
            logger.warning("ollama_pull_failed", status=resp.status_code)
    except Exception as exc:
        logger.warning("ollama_pull_exception", error=str(exc))


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors. Returns 0.0 if either is zero-length."""
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
