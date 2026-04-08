"""Rate Limit Monitor — tracks usage across all brains.

Reads known rate limit info for each brain subscription tier
and tracks actual usage to warn before hitting limits.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Persistence file for usage tracking
_USAGE_FILE = Path.home() / ".aura" / "usage.json"

# Known rate limits per brain/tier (requests or tokens per window)
BRAIN_LIMITS: Dict[str, Dict[str, Any]] = {
    "haiku": {
        "tier": "Claude Max (~$100/mo)",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "notes": "~450 Haiku msgs/5h on Max plan. Cheapest Claude subprocess.",
        "warn_threshold": 0.75,
    },
    "sonnet": {
        "tier": "Claude Max (~$100/mo)",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "notes": "~225 Sonnet msgs/5h on Max plan.",
        "warn_threshold": 0.75,
    },
    "opus": {
        "tier": "Claude Max (~$100/mo)",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "notes": "~45 Opus msgs/5h on Max plan. Use sparingly.",
        "warn_threshold": 0.60,
    },
    "codex": {
        "tier": "OpenAI Plus ($20/mo)",
        "window": "daily",
        "window_seconds": 86400,
        "notes": "Codex CLI (codex-cli 0.118.0). Subscription-based, generous limits.",
        "warn_threshold": 0.80,
    },
    "opencode": {
        "tier": "OpenRouter free",
        "window": "daily",
        "window_seconds": 86400,
        "notes": "opencode 1.3.10 via OpenRouter free tier. Model varies.",
        "warn_threshold": 0.85,
    },
    "cline": {
        "tier": "Local Ollama ($0)",
        "window": "none",
        "window_seconds": 86400,
        "notes": "Local qwen2.5:7b via Ollama. Unlimited — limited by GPU/CPU.",
        "warn_threshold": 1.0,  # never warn
    },
    "gemini": {
        "tier": "Google free",
        "window": "daily",
        "window_seconds": 86400,
        "limit": 1500,  # gemini-1.5-flash: 1500 req/day free
        "notes": "Gemini 1.5 Flash free: 1500 req/day, 15 req/min.",
        "warn_threshold": 0.80,
    },
}


@dataclass
class BrainUsage:
    """Usage stats for a single brain."""

    brain_name: str
    requests_in_window: int
    window_start: float  # timestamp
    window_seconds: int
    known_limit: Optional[int]  # None = unknown/dynamic
    last_request: float  # timestamp
    errors_in_window: int
    rate_limited_at: Optional[float]  # timestamp of last rate limit hit

    @property
    def window_remaining_seconds(self) -> int:
        """Seconds until current window resets."""
        elapsed = time.time() - self.window_start
        remaining = self.window_seconds - elapsed
        return max(0, int(remaining))

    @property
    def window_remaining_str(self) -> str:
        """Human-readable time until window reset."""
        secs = self.window_remaining_seconds
        if secs <= 0:
            return "reset"
        hours = secs // 3600
        mins = (secs % 3600) // 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"

    @property
    def usage_pct(self) -> Optional[float]:
        """Usage percentage (None if limit unknown)."""
        if self.known_limit and self.known_limit > 0:
            return min(1.0, self.requests_in_window / self.known_limit)
        return None

    @property
    def is_rate_limited(self) -> bool:
        """Whether currently rate limited."""
        if self.rate_limited_at is None:
            return False
        # Consider rate limited for 5 minutes after last hit
        return (time.time() - self.rate_limited_at) < 300

    def usage_bar(self, width: int = 10) -> str:
        """Visual usage bar."""
        pct = self.usage_pct
        if pct is None:
            return f"{self.requests_in_window} req"
        filled = int(pct * width)
        empty = width - filled
        bar = "█" * filled + "░" * empty
        return f"{bar} {int(pct * 100)}%"


class RateMonitor:
    """Tracks rate limit usage across all brains."""

    def __init__(self) -> None:
        self._usage: Dict[str, BrainUsage] = {}
        self._load()

    def _load(self) -> None:
        """Load persisted usage data."""
        try:
            if _USAGE_FILE.exists():
                data = json.loads(_USAGE_FILE.read_text())
                for name, entry in data.items():
                    self._usage[name] = BrainUsage(
                        brain_name=name,
                        requests_in_window=entry.get("requests", 0),
                        window_start=entry.get("window_start", time.time()),
                        window_seconds=entry.get("window_seconds", 3600),
                        known_limit=entry.get("known_limit"),
                        last_request=entry.get("last_request", 0),
                        errors_in_window=entry.get("errors", 0),
                        rate_limited_at=entry.get("rate_limited_at"),
                    )
        except Exception as e:
            logger.warning("rate_monitor_load_error", error=str(e))

    def _save(self) -> None:
        """Persist usage data."""
        try:
            _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for name, usage in self._usage.items():
                data[name] = {
                    "requests": usage.requests_in_window,
                    "window_start": usage.window_start,
                    "window_seconds": usage.window_seconds,
                    "known_limit": usage.known_limit,
                    "last_request": usage.last_request,
                    "errors": usage.errors_in_window,
                    "rate_limited_at": usage.rate_limited_at,
                }
            _USAGE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("rate_monitor_save_error", error=str(e))

    def _get_or_create(self, brain_name: str) -> BrainUsage:
        """Get or create usage tracker for a brain."""
        if brain_name not in self._usage:
            limits = BRAIN_LIMITS.get(brain_name, {})
            self._usage[brain_name] = BrainUsage(
                brain_name=brain_name,
                requests_in_window=0,
                window_start=time.time(),
                window_seconds=limits.get("window_seconds", 3600),
                known_limit=limits.get("limit"),
                last_request=0,
                errors_in_window=0,
                rate_limited_at=None,
            )
        return self._usage[brain_name]

    def _maybe_reset_window(self, usage: BrainUsage) -> None:
        """Reset window if expired."""
        elapsed = time.time() - usage.window_start
        if elapsed >= usage.window_seconds:
            usage.requests_in_window = 0
            usage.errors_in_window = 0
            usage.window_start = time.time()
            usage.rate_limited_at = None

    def record_request(self, brain_name: str) -> None:
        """Record a request to a brain."""
        usage = self._get_or_create(brain_name)
        self._maybe_reset_window(usage)
        usage.requests_in_window += 1
        usage.last_request = time.time()
        self._save()

    def record_error(self, brain_name: str, is_rate_limit: bool = False) -> None:
        """Record an error (optionally a rate limit hit)."""
        usage = self._get_or_create(brain_name)
        self._maybe_reset_window(usage)
        usage.errors_in_window += 1
        if is_rate_limit:
            usage.rate_limited_at = time.time()
        self._save()

    def get_usage(self, brain_name: str) -> BrainUsage:
        """Get current usage for a brain."""
        usage = self._get_or_create(brain_name)
        self._maybe_reset_window(usage)
        self._save()
        return usage

    def get_all_usage(self) -> List[BrainUsage]:
        """Get usage for all known brains (BRAIN_LIMITS only — drops stale entries)."""
        # Purge unknown keys so old names don't accumulate
        stale = [k for k in self._usage if k not in BRAIN_LIMITS]
        for k in stale:
            del self._usage[k]
        # Ensure every known brain has an entry
        for name in BRAIN_LIMITS:
            self._get_or_create(name)
        result = []
        for name in BRAIN_LIMITS:  # preserve canonical order
            usage = self._usage[name]
            self._maybe_reset_window(usage)
            result.append(usage)
        self._save()
        return result

    def should_warn(self, brain_name: str) -> Optional[str]:
        """Check if we should warn about approaching limits.

        Returns warning message or None.
        """
        usage = self.get_usage(brain_name)

        if usage.is_rate_limited:
            return f"⛔ {brain_name} rate limited. Wait ~5min."

        pct = usage.usage_pct
        if pct is not None:
            limits = BRAIN_LIMITS.get(brain_name, {})
            threshold = limits.get("warn_threshold", 0.75)
            if pct >= threshold:
                return (
                    f"⚠️ {brain_name}: {int(pct * 100)}% used "
                    f"({usage.requests_in_window}/{usage.known_limit}). "
                    f"Resets in {usage.window_remaining_str}."
                )

        return None

    def format_status(self) -> str:
        """Format all usage as Telegram HTML."""
        lines = ["<b>📊 Rate Limits</b>\n"]

        for usage in self.get_all_usage():
            limits = BRAIN_LIMITS.get(usage.brain_name, {})
            tier = limits.get("tier", "?")

            # Status icon
            if usage.is_rate_limited:
                icon = "⛔"
            elif usage.usage_pct and usage.usage_pct >= 0.75:
                icon = "⚠️"
            else:
                icon = "✅"

            bar = usage.usage_bar()
            window = limits.get("window", "?")
            reset = usage.window_remaining_str

            lines.append(
                f"{icon} <b>{usage.brain_name}</b> · {tier}\n"
                f"   {bar} · window: {window}\n"
                f"   ⏱ resets: {reset} · errors: {usage.errors_in_window}"
            )

        lines.append(
            "\n💡 Claude/Codex limits are dynamic — "
            "AURA tracks requests and warns before throttle."
        )
        return "\n".join(lines)
