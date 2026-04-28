"""Zero-token command handlers — execute without consuming AI tokens.

All handlers are mixin methods that get composed into MessageOrchestrator.
They use self.settings, self._bash_passthrough(), and self._escape_html().

This module re-exports ZeroTokenMixin, composed from focused submodules:
  - zt_system.py      — ls, pwd, git, health, terminal, context, sh, email
  - zt_workspace.py   — inbox, calendar, limits
  - zt_memory.py      — memory, costs
  - zt_brain.py       — brain, task, brains
  - zt_status.py      — dashboard, diagnose, help, status_full
  - zt_voice.py       — speak, voz
  - zt_workflow.py    — standup, report, triage, followup
  - zt_web.py         — web, search, queue
  - zt_social.py      — post, video
  - zt_integrations.py — ig_auth, posts, drive
  - zt_hermes.py      — hermes, mesh
"""

from .zt_system import ZeroTokenSystemMixin
from .zt_workspace import ZeroTokenWorkspaceMixin
from .zt_memory import ZeroTokenMemoryMixin
from .zt_brain import ZeroTokenBrainMixin
from .zt_status import ZeroTokenStatusMixin
from .zt_voice import ZeroTokenVoiceMixin
from .zt_workflow import ZeroTokenWorkflowMixin
from .zt_web import ZeroTokenWebMixin
from .zt_social import ZeroTokenSocialMixin
from .zt_integrations import ZeroTokenIntegrationsMixin
from .zt_hermes import ZeroTokenHermesMixin


class ZeroTokenMixin(
    ZeroTokenSystemMixin,
    ZeroTokenWorkspaceMixin,
    ZeroTokenMemoryMixin,
    ZeroTokenBrainMixin,
    ZeroTokenStatusMixin,
    ZeroTokenVoiceMixin,
    ZeroTokenWorkflowMixin,
    ZeroTokenWebMixin,
    ZeroTokenSocialMixin,
    ZeroTokenIntegrationsMixin,
    ZeroTokenHermesMixin,
):
    """Composed mixin: all zero-token command handlers.

    Public API is identical to the original monolithic ZeroTokenMixin.
    Submodules are internal implementation detail.
    """
