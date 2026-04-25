"""High-volume Telegram burst tests and guardrail env tuning checks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.orchestrator import MessageOrchestrator
from src.bot.orchestrator_routing import _CHAT_LAST_SQUAD, _squad_guardrail_decision
from src.config import create_test_config


class _FakeIntent:
    def __init__(self) -> None:
        self.intent = SimpleNamespace(value="CHAT")
        self.suggested_brain = "haiku"
        self.confidence = 0.9


class _FakeRouter:
    def smart_route(
        self,
        message: str,
        user_id: int,
        rate_monitor: object | None = None,
        urgent: bool = False,
    ) -> tuple[str, _FakeIntent]:
        return ("haiku", _FakeIntent())

    def get_brain(self, name: str) -> object:
        return object()


async def test_telegram_burst_preserves_order_and_trace() -> None:
    settings = create_test_config(approved_directory="/tmp", agentic_mode=True)
    deps = {
        "claude_integration": MagicMock(),
        "storage": None,
        "security_validator": None,
        "rate_limiter": None,
        "audit_logger": None,
    }
    orchestrator = MessageOrchestrator(settings, deps)

    processed: list[str] = []
    base = "continua con paso {} y valida orden y trazabilidad del flujo"
    messages = ["Nueva misión: estabilizar Telegram ráfaga"] + [
        base.format(i) for i in range(1, 26)
    ]

    async def _fake_alt_brain(
        update,  # type: ignore[no-untyped-def]
        context,  # type: ignore[no-untyped-def]
        router,  # type: ignore[no-untyped-def]
        message_text: str,
        user_id: int,
        brain_name: str = "haiku",
        intent: object | None = None,
    ) -> None:
        await asyncio.sleep(0.01)
        processed.append(update.message.text)

    async def _send_one(text: str, user_data: dict) -> None:
        update = MagicMock()
        update.effective_user.id = 999
        update.effective_chat.id = 555
        update.message.text = text
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()
        update.message.message_id = len(text)

        context = MagicMock()
        context.user_data = user_data
        context.bot_data = {"settings": settings, "brain_router": _FakeRouter()}

        await orchestrator.agentic_text(update, context)

    user_data: dict = {}
    with patch.object(orchestrator, "_handle_alt_brain", side_effect=_fake_alt_brain):
        with patch("src.infra.task_router.classify_task", new=AsyncMock(return_value=SimpleNamespace(route="simple", confidence=0.1, reason="test", source="unit"))):
            with patch("src.infra.task_router.write_external_outcome", new=lambda **kwargs: None):
                for text in messages:
                    asyncio.create_task(_send_one(text, user_data))
                    await asyncio.sleep(0)

                for _ in range(600):
                    if len(processed) == len(messages):
                        break
                    await asyncio.sleep(0.01)

    assert len(processed) == len(messages)
    assert processed == messages

    mission_state = user_data.get("mission_state", {})
    trace = user_data.get("routing_trace", [])
    assert mission_state.get("mode") in {"continue", "auto"}
    assert mission_state.get("active_prompt")
    assert len(trace) >= len(messages)


def test_guardrail_env_cooldown_override(monkeypatch) -> None:
    chat_id = 111
    _CHAT_LAST_SQUAD.clear()
    _CHAT_LAST_SQUAD[chat_id] = 100.0
    monkeypatch.setenv("AURA_SQUAD_COOLDOWN_S", "10")

    allow, reason = _squad_guardrail_decision(
        chat_id=chat_id,
        message_text=(
            "Necesito un plan completo multiagente para arquitectura, tests, "
            "rollout y recuperación de fallos."
        ),
        rate_monitor=None,
        now_ts=108.0,
    )
    assert allow is False
    assert reason == "cooldown"


def test_guardrail_env_usage_threshold_override(monkeypatch) -> None:
    class _Usage:
        def __init__(self, pct: float | None = None, rl: bool = False) -> None:
            self.usage_pct = pct
            self.is_rate_limited = rl

    class _Monitor:
        def get_usage(self, brain_name: str) -> _Usage:
            if brain_name == "sonnet":
                return _Usage(0.62, False)
            return _Usage(0.2, False)

    _CHAT_LAST_SQUAD.clear()
    monkeypatch.setenv("AURA_SQUAD_USAGE_SKIP_THRESHOLD", "0.60")

    allow, reason = _squad_guardrail_decision(
        chat_id=222,
        message_text=(
            "Necesito ejecutar una estrategia completa de coordinación de "
            "equipos con validaciones y despliegue progresivo."
        ),
        rate_monitor=_Monitor(),
        now_ts=200.0,
    )
    assert allow is False
    assert reason.startswith("brain_pressure:sonnet")
