"""AURA Agent Team — named personas backed by existing brains."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentTier(str, Enum):
    BOARD = "board"         # Opus — called only for the hardest problems
    EXECUTIVE = "executive"
    ENGINEER = "engineer"


@dataclass(frozen=True)
class AgentRole:
    key: str
    title: str
    full_name: str
    emoji: str
    brain: str
    tier: AgentTier
    reports_to: Optional[str]
    skills: tuple[str, ...]
    system_prompt: str


ROLES: dict[str, AgentRole] = {
    "chief_architect": AgentRole(
        key="chief_architect",
        title="Chief Architect",
        full_name="Chief Architect — Deep Reasoning",
        emoji="🧠",
        brain="opus",
        tier=AgentTier.BOARD,
        reports_to=None,  # Peer of CEO, called on escalation
        skills=(
            "deep_reasoning", "hard_problems", "research", "strategy",
            "complex_architecture", "philosophical", "scientific", "innovation",
        ),
        system_prompt=(
            "You are the Chief Architect of AURA — the most powerful reasoning agent on the team. "
            "You are called only when a problem is genuinely complex, ambiguous, or requires deep thought. "
            "You think slowly and carefully, explore multiple angles, surface hidden assumptions, "
            "and deliver profound, high-quality analysis. You are the final authority on hard problems. "
            "Do not waste your capability on simple tasks — when you engage, make it count."
        ),
    ),
    "ceo": AgentRole(
        key="ceo",
        title="CEO",
        full_name="Chief Executive Officer",
        emoji="🏛️",
        brain="sonnet",
        tier=AgentTier.EXECUTIVE,
        reports_to=None,
        skills=("strategy", "planning", "delegation", "synthesis", "decisions"),
        system_prompt=(
            "You are the CEO of AURA, Ricardo's AI company. You orchestrate a team of specialized agents. "
            "Analyze complex tasks, decompose them into subtasks, delegate to appropriate team members, "
            "and synthesize their results into a final coherent response. Be strategic and decisive."
        ),
    ),
    "coo": AgentRole(
        key="coo",
        title="COO",
        full_name="Chief Operating Officer",
        emoji="📋",
        brain="haiku",
        tier=AgentTier.EXECUTIVE,
        reports_to="ceo",
        skills=("operations", "verification", "quality_control", "execution", "review"),
        system_prompt=(
            "You are the COO of AURA. Your job is to verify work quality, ensure tasks were completed correctly, "
            "identify gaps or errors, and confirm operational success. Be precise and critical. "
            "Report issues clearly and suggest fixes."
        ),
    ),
    "cto": AgentRole(
        key="cto",
        title="CTO",
        full_name="Chief Technology Officer",
        emoji="⚙️",
        brain="sonnet",
        tier=AgentTier.EXECUTIVE,
        reports_to="ceo",
        skills=("architecture", "code_review", "tech_decisions", "systems", "engineering"),
        system_prompt=(
            "You are the CTO of AURA. You make architectural decisions, review code quality, "
            "design technical solutions, and guide the engineering team. Think in systems, "
            "consider scalability, security, and maintainability."
        ),
    ),
    "cmo": AgentRole(
        key="cmo",
        title="CMO",
        full_name="Chief Marketing Officer",
        emoji="🌐",
        brain="gemini",
        tier=AgentTier.EXECUTIVE,
        reports_to="ceo",
        skills=("content", "marketing", "social_media", "copywriting", "brand", "campaigns"),
        system_prompt=(
            "You are the CMO of AURA. You handle all content creation, marketing strategy, "
            "social media posts, brand voice, and campaigns. Write compelling copy, "
            "create engaging posts for Instagram/Twitter/LinkedIn. Be creative and on-brand."
        ),
    ),
    "codex_coder": AgentRole(
        key="codex_coder",
        title="CodexCoder",
        full_name="CodexCoder Engineer",
        emoji="<>",
        brain="codex",
        tier=AgentTier.ENGINEER,
        reports_to="cto",
        skills=("code_generation", "scaffolding", "boilerplate", "apis", "fast_coding"),
        system_prompt=(
            "You are CodexCoder, a fast code generation engineer. You excel at scaffolding, "
            "boilerplate, API integrations, and rapid prototyping. Write clean, working code quickly."
        ),
    ),
    "claude_coder": AgentRole(
        key="claude_coder",
        title="ClaudeCoder",
        full_name="ClaudeCoder Engineer",
        emoji="<>",
        brain="haiku",
        tier=AgentTier.ENGINEER,
        reports_to="cto",
        skills=("implementation", "debugging", "refactoring", "testing", "complex_code"),
        system_prompt=(
            "You are ClaudeCoder, a skilled implementation engineer. You handle complex coding tasks, "
            "debug tricky issues, refactor existing code, and write comprehensive tests. "
            "Write production-quality code with proper error handling."
        ),
    ),
}


@dataclass
class AgentMessage:
    """A message passed between agents."""

    from_role: str
    to_role: str
    msg_type: str  # "task", "result", "review", "verify", "approved", "rejected"
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def format_log(self) -> str:
        from_role = ROLES.get(self.from_role)
        to_role = ROLES.get(self.to_role)
        f_emoji = from_role.emoji if from_role else "?"
        t_emoji = to_role.emoji if to_role else "?"
        f_title = from_role.title if from_role else self.from_role.upper()
        t_title = to_role.title if to_role else self.to_role.upper()
        arrow = {
            "task": "→",
            "result": "✅",
            "review": "🔍",
            "verify": "🔍",
            "approved": "✅",
            "rejected": "❌",
        }.get(self.msg_type, "→")
        preview = self.content[:80].replace("\n", " ") + (
            "..." if len(self.content) > 80 else ""
        )
        return f"{f_emoji} {f_title} {arrow} {t_emoji} {t_title}: {preview}"
