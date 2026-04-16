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

# ── Module-level singleton — accessible from anywhere (conductor, brains, etc.) ──
_global_monitor: Optional["RateMonitor"] = None


def get_global_monitor() -> "RateMonitor":
    """Return the process-wide RateMonitor singleton. Creates one if needed."""
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = RateMonitor()
    return _global_monitor


def track_request(brain_name: str) -> None:
    """Record a brain request in the global monitor. Call from anywhere."""
    try:
        get_global_monitor().record_request(brain_name)
    except Exception as e:
        logger.error("track_request_failed", brain=brain_name, error=str(e), exc_info=True)


def track_error(brain_name: str, is_rate_limit: bool = False) -> None:
    """Record a brain error in the global monitor."""
    try:
        get_global_monitor().record_error(brain_name, is_rate_limit=is_rate_limit)
    except Exception as e:
        logger.error(
            "track_error_failed",
            brain=brain_name,
            is_rate_limit=is_rate_limit,
            error=str(e),
            exc_info=True,
        )


def _fmt_secs(secs: int) -> str:
    """Convert seconds to human-readable string: Xh Ym or Zm."""
    if secs <= 0:
        return "now"
    hours = secs // 3600
    mins = (secs % 3600) // 60
    secs_r = secs % 60
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs_r}s"
    return f"{secs_r}s"

# Known rate limits per brain/tier (requests or tokens per window)
BRAIN_LIMITS: Dict[str, Dict[str, Any]] = {
    "haiku": {
        "tier": "Claude Max (~$100/mo)",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "limit": 450,          # ~450 Haiku msgs/5h on Max plan (Anthropic published)
        "warn_threshold": 0.75,
    },
    "sonnet": {
        "tier": "Claude Max (~$100/mo)",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "limit": 225,          # ~225 Sonnet msgs/5h on Max plan
        "warn_threshold": 0.75,
    },
    "opus": {
        "tier": "Claude Max (~$100/mo)",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "limit": 45,           # ~45 Opus msgs/5h on Max plan
        "warn_threshold": 0.60,
    },
    "codex": {
        "tier": "OpenAI Plus ($20/mo)",
        "window": "daily",
        "window_seconds": 86400,
        "limit": 200,          # generous daily limit on Plus subscription
        "warn_threshold": 0.80,
    },
    "opencode": {
        "tier": "OpenRouter free",
        "window": "daily",
        "window_seconds": 86400,
        "limit": 50,           # conservative estimate for free tier
        "warn_threshold": 0.85,
    },
    "cline": {
        "tier": "Local Ollama ($0)",
        "window": "none",
        "window_seconds": 86400,
        "limit": None,         # unlimited — CPU/GPU bound
        "warn_threshold": 1.0,
    },
    "gemini": {
        "tier": "Google free (CLI)",
        "window": "daily",
        "window_seconds": 86400,
        "limit": 60,           # ~60 Gemini CLI calls/day free tier estimate
        "warn_threshold": 0.80,
    },
    "openrouter": {
        "tier": "OpenRouter free",
        "window": "daily",
        "window_seconds": 86400,
        "limit": 200,          # free tier models: high volume but rate-limited per model
        "warn_threshold": 0.85,
    },
    "autonomous": {
        "tier": "Claude Max + AURA MCP",
        "window": "5h rolling",
        "window_seconds": 5 * 3600,
        "limit": 225,          # uses sonnet tier — same pool as sonnet
        "warn_threshold": 0.75,
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
        """Seconds until current tracking window resets."""
        elapsed = time.time() - self.window_start
        remaining = self.window_seconds - elapsed
        return max(0, int(remaining))

    @property
    def window_remaining_str(self) -> str:
        """Human-readable time until window reset."""
        secs = self.window_remaining_seconds
        if secs <= 0:
            return "reset"
        return _fmt_secs(secs)

    @property
    def recover_at(self) -> Optional[float]:
        """Unix timestamp when rate limit should clear (end of current window).

        Returns None if not rate limited.
        """
        if self.rate_limited_at is None:
            return None
        # Rate limit clears at end of the window that was active when it hit
        return self.window_start + self.window_seconds

    @property
    def recover_in_seconds(self) -> int:
        """Seconds until rate limit clears. 0 if not rate limited or already cleared."""
        ra = self.recover_at
        if ra is None:
            return 0
        return max(0, int(ra - time.time()))

    @property
    def recover_in_str(self) -> str:
        """Human-readable time until rate limit clears."""
        secs = self.recover_in_seconds
        if secs <= 0:
            return "now"
        return _fmt_secs(secs)

    @property
    def usage_pct(self) -> Optional[float]:
        """Usage percentage (None if limit unknown)."""
        if self.known_limit and self.known_limit > 0:
            return min(1.0, self.requests_in_window / self.known_limit)
        return None

    @property
    def is_rate_limited(self) -> bool:
        """Whether currently rate limited.

        True from the moment rate_limited_at is set until the window resets.
        Adds 60s buffer after window reset to let quotas propagate.
        """
        if self.rate_limited_at is None:
            return False
        ra = self.recover_at
        if ra is None:
            return False
        # Rate limited until window ends + 60s buffer
        return time.time() < (ra + 60)

    def usage_bar(self, width: int = 10) -> str:
        """Visual usage bar — always shows bar, falls back to request count."""
        pct = self.usage_pct
        if pct is None:
            # No known limit (cline/local) — show raw request count
            return f"{self.requests_in_window} req · ilimitado"
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
                if not isinstance(data, dict):
                    logger.error("rate_monitor_load_invalid_format", expected="dict", got=type(data).__name__)
                    return
                for name, entry in data.items():
                    try:
                        if not isinstance(entry, dict):
                            logger.warning("rate_monitor_entry_invalid", brain=name, expected="dict")
                            continue
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
                        logger.warning("rate_monitor_entry_parse_failed", brain=name, error=str(e))
        except json.JSONDecodeError as e:
            logger.error("rate_monitor_load_json_error", path=str(_USAGE_FILE), error=str(e))
        except IOError as e:
            logger.error("rate_monitor_load_io_error", path=str(_USAGE_FILE), error=str(e))
        except Exception as e:
            logger.error("rate_monitor_load_unexpected_error", path=str(_USAGE_FILE), error=str(e), exc_info=True)

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
        except OSError as e:
            logger.error("rate_monitor_save_os_error", path=str(_USAGE_FILE), error=str(e))
        except json.JSONDecodeError as e:
            logger.error("rate_monitor_save_json_error", error=str(e))
        except Exception as e:
            logger.error("rate_monitor_save_unexpected_error", path=str(_USAGE_FILE), error=str(e), exc_info=True)

    def _get_or_create(self, brain_name: str) -> BrainUsage:
        """Get or create usage tracker for a brain.

        BRAIN_LIMITS is always the source of truth for known_limit —
        overrides any stale null value persisted in usage.json.
        """
        limits = BRAIN_LIMITS.get(brain_name, {})
        if brain_name not in self._usage:
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
        else:
            # Always refresh static config from BRAIN_LIMITS (source of truth)
            self._usage[brain_name].known_limit = limits.get("limit")
            self._usage[brain_name].window_seconds = limits.get("window_seconds", 3600)
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
            return f"⛔ {brain_name} rate limited. Recupera en {usage.recover_in_str}."

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
        try:
            now = time.time()
            lines = ["<b>📊 Rate Limits</b>\n"]

            for usage in self.get_all_usage():
                try:
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

                    # Last used: show real time delta
                    if usage.last_request > 0:
                        delta = int(now - usage.last_request)
                        if delta < 60:
                            last_used = f"{delta}s ago"
                        elif delta < 3600:
                            last_used = f"{delta // 60}m ago"
                        else:
                            last_used = f"{delta // 3600}h ago"
                    else:
                        last_used = "never"

                    lines.append(
                        f"{icon} <b>{usage.brain_name}</b> · {tier}\n"
                        f"   {bar} · window: {window}\n"
                        f"   ⏱ resets: {reset} · last: {last_used} · errors: {usage.errors_in_window}"
                    )
                except Exception as e:
                    logger.error("format_status_brain_error", brain=usage.brain_name, error=str(e))
                    lines.append(f"❌ {usage.brain_name} (error formatting stats)")

            lines.append(
                "\n💡 Requests tracked across conductor + direct calls."
            )
            return "\n".join(lines)
        except Exception as e:
            logger.error("format_status_fatal_error", error=str(e), exc_info=True)
            return "<b>📊 Rate Limits</b>\n❌ Error loading status data."
