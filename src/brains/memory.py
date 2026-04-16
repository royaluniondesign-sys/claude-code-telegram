"""AURA memory and learning functions."""
from datetime import datetime

from infra.utils import CONDUCTOR_LOG_PATH


def log_memory(task: str) -> None:
    """Log a task to memory."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CONDUCTOR_LOG_PATH, 'a') as log_file:
            log_file.write(f"{timestamp},{task}\n")
    except Exception:
        pass
