"""AURA Conductor — orchestrates proactive loop learning."""

from src.infra import memory


def run_learning(timestamp: str, task_title: str, steps_ok: int, duration: str, committed: bool) -> None:
    """Execute conductor learning: run proactive loop and persist results."""
    # Write learning to memory
    memory.append_to_log(timestamp, task_title, steps_ok, duration, committed)
