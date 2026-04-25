"""Cross-system smoke tests for Telegram runtime, dashboard API, routines, RAG and memory."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    from src.context import mempalace_memory

    class _FakeCollection:
        def __init__(self) -> None:
            self.docs: list[str] = []
            self.metas: list[dict] = []
            self.ids: list[str] = []

        def count(self) -> int:
            return len(self.docs)

        def add(self, documents: list[str], ids: list[str], metadatas: list[dict]) -> None:
            self.docs.extend(documents)
            self.ids.extend(ids)
            self.metas.extend(metadatas)

        def query(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"documents": [self.docs], "distances": [[0.2 for _ in self.docs]]}

        def get(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"documents": self.docs, "metadatas": self.metas, "ids": self.ids}

        def delete(self, ids: list[str]) -> None:
            keep = [(d, m, i) for d, m, i in zip(self.docs, self.metas, self.ids) if i not in ids]
            self.docs = [x[0] for x in keep]
            self.metas = [x[1] for x in keep]
            self.ids = [x[2] for x in keep]

    fake = _FakeCollection()
    monkeypatch.setattr(mempalace_memory, "_get_collection", lambda: fake)

    await mempalace_memory.store_interaction(
        "Necesito estabilizar Telegram con menos coste",
        "Haré routing inteligente y pruebas de ráfaga.",
    )
    hits = await mempalace_memory.search_memories("estabilizar telegram", n=3)
    formatted = mempalace_memory.format_memories_for_prompt(hits)

    assert len(hits) >= 1
    assert "Memoria" in formatted
