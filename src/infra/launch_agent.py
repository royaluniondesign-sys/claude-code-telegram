import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class LaunchAgent:
    """Manages bot process lifecycle with keepalive via LaunchAgent plist.

    Restarts the bot process if it exits unexpectedly, with throttle interval
    to avoid rapid restart loops.
    """

    def __init__(self) -> None:
        self.process: Optional[asyncio.subprocess.Process] = None
        self.throttle_interval = 10

    async def start(self) -> None:
        """Start the bot process."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                "/Users/oxyzen/claude-code-telegram/bin/aura",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("Bot process started (PID: %s)", self.process.pid)
        except Exception as e:
            logger.error("Failed to start bot process: %s", e)
            raise

    def stop(self) -> None:
        """Stop the bot process."""
        if self.process and not self.process.returncode:
            self.process.terminate()
            logger.info("Bot process terminated")

    async def restart_loop(self) -> None:
        """Continuously monitor and restart bot if it exits."""
        while True:
            try:
                await self.start()
                await self.process.wait()
                logger.warning("Bot process exited with code %s, restarting in %ds",
                             self.process.returncode, self.throttle_interval)
                await asyncio.sleep(self.throttle_interval)
            except Exception as e:
                logger.exception("Exception in restart loop: %s", e)
                await asyncio.sleep(self.throttle_interval)


def ensure_launch_agent_is_running() -> bool:
    """Ensure LaunchAgent plist is installed and running.

    Returns:
        True if LaunchAgent is running, False otherwise.
    """
    plist_src = Path("/Users/oxyzen/claude-code-telegram/src/infra/com.aura.bot.plist")
    plist_dest = Path.home() / "Library" / "LaunchAgents" / "com.aura.bot.plist"

    if not plist_src.exists():
        logger.error("LaunchAgent plist not found at %s", plist_src)
        return False

    try:
        # Create LaunchAgents directory if needed
        plist_dest.parent.mkdir(parents=True, exist_ok=True)

        # Copy plist to LaunchAgents
        shutil.copy2(plist_src, plist_dest)
        logger.info("LaunchAgent plist installed to %s", plist_dest)

        # Load plist with launchctl
        subprocess.run(
            ["launchctl", "load", str(plist_dest)],
            check=False,
            capture_output=True,
        )
        logger.info("LaunchAgent loaded: com.aura.bot")
        return True
    except Exception as e:
        logger.error("Failed to install LaunchAgent: %s", e)
        return False
