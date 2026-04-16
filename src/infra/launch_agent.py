"""LaunchAgent auto-restart — ensures AURA bot stays alive.

Checks LaunchAgent status every periodic interval. If not running, restarts it.
"""

import subprocess
from typing import Optional


def check_launch_agent_status() -> bool:
    """Check if LaunchAgent (com.aura.telegram-bot) is currently running.

    Returns:
        True if running, False otherwise.
    """
    try:
        result = subprocess.run(
            ['launchctl', 'list', 'com.aura.telegram-bot'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if "com.aura.telegram-bot" in result.stdout:
            return True
        else:
            return False
    except Exception as e:
        print(f"Failed to check LaunchAgent status: {e}")
        return False


def restart_launch_agent() -> None:
    """Restart the LaunchAgent service (com.aura.telegram-bot).

    Unloads and reloads the service. If restart fails, exception is logged.
    """
    try:
        subprocess.run(
            ['launchctl', 'unload', 'com.aura.telegram-bot'],
            check=True,
            timeout=5
        )
        subprocess.run(
            ['launchctl', 'load', 'com.aura.telegram-bot'],
            check=True,
            timeout=5
        )
    except Exception as e:
        print(f"Failed to restart LaunchAgent: {e}")


def ensure_launch_agent_is_running() -> None:
    """Ensure LaunchAgent (com.aura.telegram-bot) is running, restart if needed."""
    if not check_launch_agent_status():
        restart_launch_agent()
