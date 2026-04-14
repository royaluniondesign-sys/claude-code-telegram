"""Agent Activity Tracker — real-time state of the squad."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional
import structlog

logger = structlog.get_logger()


@dataclass
class AgentState:
    key: str
    title: str
    emoji: str
    brain: str
    status: str = "idle"        # idle | thinking | working | done | error
    current_task: str = ""
    last_output: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    task_count: int = 0

    def elapsed_ms(self) -> Optional[int]:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["elapsed_ms"] = self.elapsed_ms()
        return d


@dataclass
class AgentMessage:
    from_key: str
    to_key: str
    text: str
    msg_type: str = "info"   # task | result | debate | review | approved | rejected
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class ActivityTracker:
    """Singleton tracking all agent states and inter-agent messages."""

    _MAX_MESSAGES = 100  # rolling window
    _MAX_RUNS = 10       # keep last N run results

    def __init__(self) -> None:
        from src.agents.team import ROLES
        self._agents: dict[str, AgentState] = {
            key: AgentState(
                key=key,
                title=role.title,
                emoji=role.emoji,
                brain=role.brain,
            )
            for key, role in ROLES.items()
        }
        self._messages: list[AgentMessage] = []
        self._run_active: bool = False
        self._run_started: Optional[float] = None
        self._run_task: str = ""
        # Full result storage
        self._last_result: str = ""
        self._last_result_ts: Optional[float] = None
        self._run_history: list[dict] = []  # last N completed runs
        self._stop_requested: bool = False

    def start_run(self, task: str) -> None:
        self._run_active = True
        self._run_started = time.time()
        self._run_task = task[:120]
        # Reset all to idle
        for agent in self._agents.values():
            agent.status = "idle"
            agent.current_task = ""
            agent.last_output = ""
            agent.started_at = None
            agent.finished_at = None
        self._messages.clear()
        logger.info("squad_run_start", task=self._run_task)

    def end_run(self, result: str = "") -> None:
        duration_ms = int((time.time() - (self._run_started or time.time())) * 1000)
        self._run_active = False
        self._stop_requested = False
        for agent in self._agents.values():
            if agent.status in ("working", "thinking"):
                agent.status = "idle"
        if result:
            self._last_result = result
            self._last_result_ts = time.time()
            # Add to history
            self._run_history.append({
                "task": self._run_task,
                "result": result,
                "duration_ms": duration_ms,
                "ts": self._last_result_ts,
                "agents_used": [
                    k for k, ag in self._agents.items() if ag.task_count > 0
                ],
            })
            if len(self._run_history) > self._MAX_RUNS:
                self._run_history = self._run_history[-self._MAX_RUNS:]
        logger.info("squad_run_end", duration_ms=duration_ms)

    def request_stop(self) -> None:
        """Signal the running squad to stop after current task."""
        self._stop_requested = True
        logger.info("squad_stop_requested")

    def should_stop(self) -> bool:
        return self._stop_requested

    def set_working(self, agent_key: str, task: str) -> None:
        ag = self._agents.get(agent_key)
        if not ag:
            return
        ag.status = "working"
        ag.current_task = task[:100]
        ag.started_at = time.time()
        ag.finished_at = None
        logger.info(
            "agent_working",
            agent=agent_key,
            title=ag.title,
            task=ag.current_task,
        )

    def set_thinking(self, agent_key: str, task: str = "") -> None:
        ag = self._agents.get(agent_key)
        if not ag:
            return
        ag.status = "thinking"
        ag.current_task = task[:100] if task else ag.current_task
        ag.started_at = ag.started_at or time.time()
        logger.info("agent_thinking", agent=agent_key, title=ag.title)

    def set_done(self, agent_key: str, output: str = "") -> None:
        ag = self._agents.get(agent_key)
        if not ag:
            return
        ag.status = "done"
        ag.last_output = output[:200]
        ag.finished_at = time.time()
        ag.task_count += 1
        logger.info(
            "agent_done",
            agent=agent_key,
            title=ag.title,
            elapsed_ms=ag.elapsed_ms(),
        )

    def set_error(self, agent_key: str, error: str = "") -> None:
        ag = self._agents.get(agent_key)
        if not ag:
            return
        ag.status = "error"
        ag.last_output = error[:150]
        ag.finished_at = time.time()
        logger.warning("agent_error", agent=agent_key, error=error[:80])

    def add_message(
        self,
        from_key: str,
        to_key: str,
        text: str,
        msg_type: str = "info",
    ) -> None:
        msg = AgentMessage(
            from_key=from_key,
            to_key=to_key,
            text=text[:300],
            msg_type=msg_type,
        )
        self._messages.append(msg)
        if len(self._messages) > self._MAX_MESSAGES:
            self._messages = self._messages[-self._MAX_MESSAGES:]
        logger.info(
            "agent_message",
            from_agent=from_key,
            to_agent=to_key,
            msg_type=msg_type,
            preview=text[:60],
        )

    def snapshot(self) -> dict:
        return {
            "run_active": self._run_active,
            "run_task": self._run_task,
            "run_started": self._run_started,
            "stop_requested": self._stop_requested,
            "agents": {k: v.to_dict() for k, v in self._agents.items()},
            "messages": [m.to_dict() for m in self._messages[-50:]],
            "last_result": self._last_result,
            "last_result_ts": self._last_result_ts,
            "run_history": self._run_history[-5:],  # last 5 for dashboard
        }


_tracker: Optional[ActivityTracker] = None


def get_tracker() -> ActivityTracker:
    global _tracker
    if _tracker is None:
        _tracker = ActivityTracker()
    return _tracker
