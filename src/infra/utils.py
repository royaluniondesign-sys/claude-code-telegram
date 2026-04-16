"""AURA infrastructure utilities."""
from pathlib import Path
import requests
import backoff
from requests.exceptions import RequestException

CONDUCTOR_LOG_PATH = Path.home() / ".aura" / "memory" / "conductor_log.md"


def send_telegram_message(token: str, chat_id: int | str, message: str) -> dict:
    """Send a message to Telegram with retry logic on failures.

    Args:
        token: Telegram bot token
        chat_id: Telegram chat ID
        message: Message text to send

    Returns:
        JSON response from Telegram API

    Raises:
        RequestException: If message fails after max retries
    """
    @backoff.on_exception(backoff.expo, RequestException, max_tries=5)
    def _send_with_retry() -> dict:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message}
        )
        response.raise_for_status()
        return response.json()

    return _send_with_retry()
