"""AURA Memory — conductor run learning persistence."""

from pathlib import Path


def append_to_log(timestamp: str, task_title: str, steps_ok: int, duration: str, committed: bool) -> None:
    """Append conductor run learning to ~/.aura/memory/conductor_log.md."""
    memory_dir = Path.home() / ".aura" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    log_file = memory_dir / "conductor_log.md"
    with open(log_file, "a") as f:
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Task Title: {task_title}\n")
        f.write(f"Steps OK: {steps_ok}\n")
        f.write(f"Duration: {duration}\n")
        f.write(f"Committed: {committed}\n\n")
