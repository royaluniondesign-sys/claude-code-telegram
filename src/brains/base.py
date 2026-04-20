"""Abstract Brain interface — contract for all LLM backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BrainStatus(Enum):
    """Health status of a brain."""

    READY = "ready"
    NOT_INSTALLED = "not_installed"       # binary/service not running
    UNREACHABLE = "unreachable"           # host exists but network can't reach it
    NOT_AUTHENTICATED = "not_authenticated"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class BrainResponse:
    """Immutable response from any brain."""

    content: str
    brain_name: str
    cost: float = 0.0
    duration_ms: int = 0
    is_error: bool = False
    error_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class Brain(ABC):
    """Abstract interface for LLM backends.

    All brains must implement execute() and health_check().
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier: 'claude', 'codex', 'gemini'."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for Telegram display."""
        ...

    @property
    @abstractmethod
    def emoji(self) -> str:
        """Emoji icon for this brain."""
        ...

    @abstractmethod
    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 300,
    ) -> BrainResponse:
        """Execute a prompt and return the response."""
        ...

    @abstractmethod
    async def health_check(self) -> BrainStatus:
        """Check if this brain is available and authenticated."""
        ...

    @abstractmethod
    async def get_info(self) -> Dict[str, Any]:
        """Return info about this brain (version, auth, limits)."""
        ...
