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


async def embed_batch(texts: List[str]) -> List[Optional[np.ndarray]]:
    """Embed multiple texts in a single Ollama API call.

    Returns list of arrays (None where embedding failed).
    """
    if not texts:
        return []

    # Check cache first
    results: List[Optional[np.ndarray]] = []
    uncached_indices: List[int] = []
    uncached_texts: List[str] = []

    for i, text in enumerate(texts):
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

            if resp.status_code != 200:
                logger.error("ollama_embed_error", status=resp.status_code, body=resp.text[:200])
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
