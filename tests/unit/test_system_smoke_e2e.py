"""Cross-system smoke tests for Telegram runtime, dashboard API, routines, RAG and memory."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.events.bus import EventBus
from src.scheduler import routine_runner


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.development_mode = True
    settings.github_webhook_secret = "gh-secret"
    settings.webhook_api_secret = "api-secret"
    settings.api_server_port = 8080
    settings.debug = False
    return settings


class _FakeBrain:
    name = "haiku"
    display_name = "Claude Haiku"

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 300,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            is_error=False,
            content=f"ok:{prompt[:20]}",
            error_type=None,
            cost=0.0,
        )


class _FakeRouter:
    def __init__(self) -> None:
        self._brain = _FakeBrain()

    def get_brain(self, name: str) -> _FakeBrain:
        return self._brain

    def get_default_brain(self) -> _FakeBrain:
        return self._brain

    def smart_route(self, message: str, rate_monitor: object | None = None) -> tuple[str, str]:
        return ("haiku", "smoke")


def test_dashboard_api_chat_smoke() -> None:
    # DASHBOARD_TOKEN may be set in the process env (e.g. loaded from .env by
    # test_config.py tests). Clear it during app creation so the auth middleware
    # allows unauthenticated requests in this isolated smoke test.
    with patch.dict(os.environ, {"DASHBOARD_TOKEN": ""}):
        app = create_api_app(EventBus(), _make_settings(), brain_router=_FakeRouter())
    client = TestClient(app)

    res = client.post("/api/chat", json={"message": "run quick check", "brain": "haiku"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["brain"] == "haiku"
    assert "ok:run quick check" in body["content"]


async def test_routine_runner_background_dedup_and_result(monkeypatch) -> None:
    routine = SimpleNamespace(
        id="r1",
        name="smoke routine",
        prompt="run tests",
        brain="haiku",
        working_dir="/tmp",
        auto_created=False,
        run_count=0,
    )

    logs: list[tuple[str, str]] = []

    async def _get_routine(rid: str) -> SimpleNamespace | None:
        return routine if rid == "r1" else None

    async def _update_routine(_rid: str, **fields: object) -> SimpleNamespace:
        for k, v in fields.items():
            setattr(routine, k, v)
        return routine

    async def _append_log(rid: str, status: str, output: str, duration_ms: int, brain_used: str) -> None:
        logs.append((rid, status))

    monkeypatch.setattr(routine_runner, "get_routine", _get_routine)
    monkeypatch.setattr(routine_runner, "update_routine", _update_routine)
    monkeypatch.setattr(routine_runner, "append_log", _append_log)
    monkeypatch.setattr(routine_runner, "_brain_router", _FakeRouter())
    routine_runner._jobs.clear()

    job1 = await routine_runner.run_routine_background("r1")
    job2 = await routine_runner.run_routine_background("r1")
    assert job1 == job2

    for _ in range(80):
        status = routine_runner.get_job_status(job1)
        if status and status.get("status") in ("ok", "error"):
            break
        import asyncio

        await asyncio.sleep(0.01)

    status = routine_runner.get_job_status(job1)
    assert status is not None
    assert status["status"] == "ok"
    assert logs and logs[-1] == ("r1", "ok")


async def test_self_healer_diagnostics_smoke(monkeypatch) -> None:
    from src.infra import self_healer

    async def _ok(report):  # type: ignore[no-untyped-def]
        report.fixed("rotated logs")

    async def _warn(report):  # type: ignore[no-untyped-def]
        report.warn("soft warning")

    monkeypatch.setattr(self_healer, "_check_bot_process", _ok)
    monkeypatch.setattr(self_healer, "_check_disk", _ok)
    monkeypatch.setattr(self_healer, "_check_ram", _warn)
    monkeypatch.setattr(self_healer, "_check_env_vars", _ok)
    monkeypatch.setattr(self_healer, "_check_log_errors", _ok)
    monkeypatch.setattr(self_healer, "_check_error_patterns", _ok)
    monkeypatch.setattr(self_healer, "_check_log_size", _ok)
    monkeypatch.setattr(self_healer, "_check_mem0", _ok)
    monkeypatch.setattr(self_healer, "_check_brains", _ok)

    report = await self_healer.run_diagnostics()
    assert report.ok is True
    assert "soft warning" in report.warnings
    assert "rotated logs" in report.fixes_applied


async def test_rag_indexer_incremental_smoke(tmp_path: Path, monkeypatch) -> None:
    from src.rag import indexer as rag_indexer_mod

    class _FakeStore:
        def __init__(self) -> None:
            self.hashes: dict[str, str] = {}

        async def init(self) -> None:
            return None

        async def get_chunk_hashes(self, source: str) -> dict[str, str]:
            return dict(self.hashes)

        async def upsert_chunk(
            self,
            id: str,
            source: str,
            source_type: str,
            content: str,
            embedding: list[float],
            metadata: dict,
        ) -> None:
            self.hashes[id] = hashlib.sha256(content.encode()).hexdigest()[:32]

    async def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(rag_indexer_mod, "embed_batch", _fake_embed_batch)
    idx = rag_indexer_mod.RAGIndexer()
    idx._store = _FakeStore()

    p = tmp_path / "mission.md"
    p.write_text("# Mission\nImprove Telegram stability\n", encoding="utf-8")

    first = await idx.index_file(p, "mission")
    second = await idx.index_file(p, "mission")
    p.write_text("# Mission\nImprove Telegram stability\nAdd dashboard checks\n", encoding="utf-8")
    third = await idx.index_file(p, "mission")

    assert first["indexed"] > 0
    assert second["indexed"] == 0
    assert second["skipped"] > 0
    assert third["indexed"] > 0


async def test_memory_layer_store_and_search_smoke(monkeypatch) -> None:
    """Mempalace now routes to RAG — verify calls reach the RAG layer without error."""
    from src.context import mempalace_memory

    indexed_calls: list[str] = []
    searched_calls: list[str] = []

    async def _fake_index_text(text: str, source: str, source_type: str) -> dict:
        indexed_calls.append(text)
        return {"indexed": 1, "skipped": 0, "errors": 0}

    async def _fake_search(query: str, top_k: int = 5, **kwargs) -> list:
        searched_calls.append(query)
        return [{"content": f"Memoria relevante sobre: {query}", "score": 0.9}]

    # Patch at the RAG layer so no Ollama/SQLite needed in CI
    from src.rag import indexer as _idx_mod, retriever as _ret_mod

    class _FakeIndexer:
        async def index_text(self, text: str, source: str, source_type: str) -> dict:
            return await _fake_index_text(text, source, source_type)

    class _FakeRetriever:
        async def search(self, query: str, top_k: int = 5, **kwargs) -> list:
            return await _fake_search(query, top_k=top_k)

    monkeypatch.setattr(_idx_mod, "RAGIndexer", _FakeIndexer)
    monkeypatch.setattr(_ret_mod, "RAGRetriever", _FakeRetriever)

    await mempalace_memory.store_interaction(
        "Necesito estabilizar Telegram con menos coste",
        "Haré routing inteligente y pruebas de ráfaga.",
    )
    hits = await mempalace_memory.search_memory("estabilizar telegram", top_k=3)

    assert len(indexed_calls) == 1
    assert "estabilizar" in indexed_calls[0].lower() or "routing" in indexed_calls[0].lower()
    assert len(hits) >= 1
