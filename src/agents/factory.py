"""AgentFactory — dynamic agent creation & matryoshka orchestration.

Any agent can spawn sub-agents. If a role doesn't exist in the catalog,
the factory synthesizes it on the fly: generates system_prompt + assigns
the optimal brain based on skill domain.

Matryoshka pattern (depth-limited to 3):

  Task → CEO (Sonnet) decomposes
    ├─ Copywriter [synthesized] (qwen-code)
    │    └─ SEO Analyst [synthesized] (gemini)
    ├─ Designer Brief [synthesized] (qwen-code)
    └─ Backend Engineer [synthesized] (ollama-rud)
         └─ Test Writer [synthesized] (haiku)

Each agent responds with structured output. If it needs a sub-agent,
it includes a `__SPAWN__` block. Factory detects it, spawns, feeds back.

Brain assignment by skill domain:
  creative/copy/design/ux    → qwen-code  (1000/day, multilingual)
  research/seo/web/trends    → gemini     (real web access)
  code/backend/devops        → ollama-rud (unlimited free, code-focused)
  frontend/mobile            → qwen-code
  qa/testing/review          → haiku      (fast, reliable)
  strategy/product/legal     → sonnet     (complex reasoning)
  deep_analysis/architecture → opus       (escalation only)
  operations/verification    → haiku
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

MAX_DEPTH = 3  # Matryoshka recursion limit

# ── Brain assignment by skill keyword ────────────────────────────────────────
# Maps skill/domain keywords → brain name.
# First match wins (ordered by specificity).
_SKILL_BRAIN: list[tuple[str, str]] = [
    # Deep reasoning — Opus (expensive, only for hard problems)
    ("deep_reasoning",    "opus"),
    ("architecture",      "sonnet"),
    ("strategy",          "sonnet"),
    ("product_strategy",  "sonnet"),
    ("legal",             "sonnet"),
    ("compliance",        "sonnet"),
    # Web-aware research — Gemini
    ("research",          "gemini"),
    ("seo",               "gemini"),
    ("web_analysis",      "gemini"),
    ("trends",            "gemini"),
    ("competitor",        "gemini"),
    ("market",            "gemini"),
    # Code/engineering — Ollama RUD (free, unlimited)
    ("backend",           "ollama-rud"),
    ("api",               "ollama-rud"),
    ("database",          "ollama-rud"),
    ("devops",            "ollama-rud"),
    ("infrastructure",    "ollama-rud"),
    ("code",              "ollama-rud"),
    ("engineering",       "ollama-rud"),
    # Frontend / creative code — Qwen
    ("frontend",          "qwen-code"),
    ("ui",                "qwen-code"),
    ("mobile",            "qwen-code"),
    # Creative / content — Qwen (1000/day, multilingual)
    ("copy",              "qwen-code"),
    ("copywriting",       "qwen-code"),
    ("content",           "qwen-code"),
    ("design",            "qwen-code"),
    ("ux",                "qwen-code"),
    ("brand",             "qwen-code"),
    ("social",            "gemini"),
    ("marketing",         "gemini"),
    ("email_marketing",   "qwen-code"),
    ("data",              "qwen-code"),
    ("analytics",         "qwen-code"),
    # QA / ops — Haiku (fast, reliable)
    ("testing",           "haiku"),
    ("qa",                "haiku"),
    ("quality",           "haiku"),
    ("review",            "haiku"),
    ("verification",      "haiku"),
    ("operations",        "haiku"),
    # Default
    ("general",           "qwen-code"),
]

# Role name → skill keywords (for unknown roles, inferred from name)
_ROLE_NAME_SKILLS: list[tuple[str, list[str]]] = [
    ("copywriter",       ["copy", "copywriting"]),
    ("copy",             ["copy", "copywriting"]),
    ("designer",         ["design", "ux"]),
    ("ux",               ["ux", "design"]),
    ("ui",               ["ui", "frontend"]),
    ("seo",              ["seo", "research"]),
    ("researcher",       ["research"]),
    ("analyst",          ["analytics", "data"]),
    ("data",             ["data", "analytics"]),
    ("devops",           ["devops", "infrastructure"]),
    ("backend",          ["backend", "code"]),
    ("frontend",         ["frontend", "ui"]),
    ("engineer",         ["code", "engineering"]),
    ("developer",        ["code", "engineering"]),
    ("qa",               ["qa", "testing"]),
    ("tester",           ["testing", "qa"]),
    ("marketer",         ["marketing", "social"]),
    ("social",           ["social", "marketing"]),
    ("product",          ["product_strategy", "strategy"]),
    ("pm",               ["product_strategy", "strategy"]),
    ("strategist",       ["strategy"]),
    ("legal",            ["legal", "compliance"]),
    ("compliance",       ["compliance", "legal"]),
    ("writer",           ["copy", "content"]),
    ("editor",           ["copy", "review"]),
    ("architect",        ["architecture", "strategy"]),
    ("manager",          ["operations", "strategy"]),
]


def _infer_skills_from_role(role_name: str) -> list[str]:
    """Infer skill keywords from a role name string."""
    name_lower = role_name.lower().replace("_", " ").replace("-", " ")
    for trigger, skills in _ROLE_NAME_SKILLS:
        if trigger in name_lower:
            return skills
    return ["general"]


def _assign_brain(skills: list[str]) -> str:
    """Choose the optimal brain for a given skill list."""
    for skill in skills:
        for keyword, brain in _SKILL_BRAIN:
            if keyword in skill.lower():
                return brain
    return "qwen-code"  # default: free, capable


def _synthesize_system_prompt(role_name: str, skills: list[str], task_hint: str = "") -> str:
    """Generate a system prompt for a dynamically created role."""
    skills_str = ", ".join(skills).replace("_", " ")
    name_clean = role_name.replace("_", " ").title()
    task_note = f" Your current focus: {task_hint[:120]}." if task_hint else ""
    return (
        f"You are {name_clean} at AURA, Ricardo's AI agency. "
        f"Your expertise: {skills_str}. "
        f"You are precise, professional, and deliver exactly what's asked.{task_note} "
        f"If you need information from another specialist, clearly state what you need "
        f"at the end of your response using this format:\n"
        f"__NEEDS__: [role_needed] → [specific question or task]\n"
        f"Only request sub-agents when truly necessary. Prefer completing the task yourself."
    )


# ── Dynamic Agent ─────────────────────────────────────────────────────────────

@dataclass
class SynthesizedRole:
    """A role created on the fly by the factory."""
    key: str
    title: str
    emoji: str
    brain: str
    skills: list[str]
    system_prompt: str


@dataclass
class AgentResult:
    """Result from a dynamic agent execution."""
    role: str
    brain: str
    content: str
    sub_results: list["AgentResult"] = field(default_factory=list)
    duration_ms: int = 0
    is_error: bool = False

    def flat_content(self) -> str:
        """Return content with all sub-results integrated."""
        parts = [self.content]
        for sub in self.sub_results:
            parts.append(
                f"\n[{sub.role.upper()} input]\n{sub.flat_content()}"
            )
        return "\n".join(parts)


class DynamicAgent:
    """An agent that can execute a task AND spawn sub-agents (matryoshka)."""

    def __init__(
        self,
        role: SynthesizedRole,
        brain_router: Any,
        factory: "AgentFactory",
        notify_fn: Optional[Callable[[str], Any]] = None,
        depth: int = 0,
    ) -> None:
        self._role = role
        self._router = brain_router
        self._factory = factory
        self._notify = notify_fn
        self._depth = depth

    async def _notify_safe(self, msg: str) -> None:
        if self._notify:
            try:
                await self._notify(msg)
            except Exception:
                pass

    async def execute(self, task: str, context: str = "") -> AgentResult:
        """Execute task. If response requests sub-agents, spawn them (matryoshka)."""
        start = time.time()

        brain = self._router.get_brain(self._role.brain)
        if brain is None:
            brain = self._router.get_brain("qwen-code") or self._router.get_brain("haiku")
        if brain is None:
            return AgentResult(
                role=self._role.key,
                brain=self._role.brain,
                content="[No brain available]",
                is_error=True,
            )

        indent = "  " * self._depth
        depth_marker = f"[L{self._depth}]" if self._depth > 0 else ""
        await self._notify_safe(
            f"{indent}{self._role.emoji} <b>{self._role.title}</b> {depth_marker} trabajando..."
        )

        context_block = f"[Context from team]\n{context}\n\n" if context else ""
        full_prompt = (
            f"[SYSTEM: {self._role.system_prompt}]\n\n"
            f"{context_block}"
            f"[TASK]\n{task}"
        )

        try:
            result = await asyncio.wait_for(
                brain.execute(prompt=full_prompt),
                timeout=120,
            )
            content = result.content if hasattr(result, "content") else str(result)
            if result.is_error if hasattr(result, "is_error") else False:
                content = f"[{self._role.title} error: {content}]"
        except asyncio.TimeoutError:
            content = f"[{self._role.title} timed out]"
        except Exception as exc:
            content = f"[{self._role.title} error: {exc}]"

        elapsed = int((time.time() - start) * 1000)

        # ── Matryoshka: detect sub-agent requests ────────────────────────────
        sub_results: list[AgentResult] = []
        if self._depth < MAX_DEPTH:
            needs_pattern = re.compile(
                r"__NEEDS__:\s*\[?([^\]\n→]+)\]?\s*→\s*(.+?)(?=__NEEDS__|$)",
                re.IGNORECASE | re.DOTALL,
            )
            for match in needs_pattern.finditer(content):
                sub_role_name = match.group(1).strip()
                sub_task = match.group(2).strip()[:500]

                await self._notify_safe(
                    f"{indent}  ↳ {self._role.title} invoca <b>{sub_role_name}</b>..."
                )

                sub_agent = self._factory.spawn(
                    role_name=sub_role_name,
                    task_hint=sub_task,
                    depth=self._depth + 1,
                    notify_fn=self._notify,
                )
                sub_result = await sub_agent.execute(sub_task, context=content)
                sub_results.append(sub_result)

                # Feed sub-agent result back into content
                content = content.replace(match.group(0), "").strip()
                content += f"\n\n[{sub_role_name.upper()} provided]\n{sub_result.flat_content()}"

        await self._notify_safe(
            f"{indent}✅ <b>{self._role.title}</b> completó ({elapsed}ms)"
        )

        return AgentResult(
            role=self._role.key,
            brain=self._role.brain,
            content=content,
            sub_results=sub_results,
            duration_ms=elapsed,
        )


# ── Agent Factory ─────────────────────────────────────────────────────────────

class AgentFactory:
    """Creates agents on demand. Synthesizes unknown roles automatically.

    Known roles (from team.ROLES) are used as-is.
    Unknown roles get: inferred skills → optimal brain → auto system_prompt.
    Each agent can spawn sub-agents up to MAX_DEPTH levels deep.
    """

    def __init__(self, brain_router: Any) -> None:
        self._router = brain_router
        self._synthesized: dict[str, SynthesizedRole] = {}
        self._load_known_roles()
        logger.info("agent_factory_ready", known=len(self._synthesized))

    def _load_known_roles(self) -> None:
        """Load existing team roles into factory catalog."""
        try:
            from .team import ROLES
            for key, role in ROLES.items():
                self._synthesized[key] = SynthesizedRole(
                    key=key,
                    title=role.title,
                    emoji=role.emoji,
                    brain=role.brain,
                    skills=list(role.skills),
                    system_prompt=role.system_prompt,
                )
        except Exception as e:
            logger.warning("factory_team_load_failed", error=str(e))

    def get_or_create_role(self, role_name: str, task_hint: str = "") -> SynthesizedRole:
        """Return existing role or synthesize a new one."""
        key = role_name.lower().strip().replace(" ", "_").replace("-", "_")

        if key in self._synthesized:
            return self._synthesized[key]

        # Synthesize new role
        skills = _infer_skills_from_role(role_name)
        brain = _assign_brain(skills)
        emoji = self._pick_emoji(skills)
        title = role_name.replace("_", " ").title()
        system_prompt = _synthesize_system_prompt(role_name, skills, task_hint)

        role = SynthesizedRole(
            key=key,
            title=title,
            emoji=emoji,
            brain=brain,
            skills=skills,
            system_prompt=system_prompt,
        )
        self._synthesized[key] = role
        logger.info(
            "agent_synthesized",
            role=key,
            brain=brain,
            skills=skills,
        )
        return role

    def _pick_emoji(self, skills: list[str]) -> str:
        skill_str = " ".join(skills)
        if any(s in skill_str for s in ["design", "ux", "ui"]):       return "🎨"
        if any(s in skill_str for s in ["copy", "content", "write"]): return "✍️"
        if any(s in skill_str for s in ["code", "backend", "dev"]):   return "💻"
        if any(s in skill_str for s in ["research", "seo", "web"]):   return "🔍"
        if any(s in skill_str for s in ["data", "analytics"]):        return "📊"
        if any(s in skill_str for s in ["marketing", "social"]):      return "📣"
        if any(s in skill_str for s in ["qa", "testing", "quality"]): return "🧪"
        if any(s in skill_str for s in ["strategy", "product"]):      return "🗺️"
        if any(s in skill_str for s in ["legal", "compliance"]):      return "⚖️"
        if any(s in skill_str for s in ["devops", "infra"]):          return "🔧"
        return "🤖"

    def spawn(
        self,
        role_name: str,
        task_hint: str = "",
        depth: int = 0,
        notify_fn: Optional[Callable[[str], Any]] = None,
    ) -> DynamicAgent:
        """Spawn a DynamicAgent for the given role (creates if unknown)."""
        role = self.get_or_create_role(role_name, task_hint)
        return DynamicAgent(
            role=role,
            brain_router=self._router,
            factory=self,
            notify_fn=notify_fn,
            depth=depth,
        )

    def list_roles(self) -> list[dict]:
        """Return all known + synthesized roles."""
        return [
            {
                "key": r.key,
                "title": r.title,
                "emoji": r.emoji,
                "brain": r.brain,
                "skills": r.skills,
                "synthesized": r.key not in self._get_known_keys(),
            }
            for r in self._synthesized.values()
        ]

    def _get_known_keys(self) -> set[str]:
        try:
            from .team import ROLES
            return set(ROLES.keys())
        except Exception:
            return set()

    def format_org_chart(self) -> str:
        """Telegram-friendly org chart of all agents."""
        known_keys = self._get_known_keys()
        lines = ["<b>🏢 AURA Agent Factory</b>", ""]

        known = [r for r in self._synthesized.values() if r.key in known_keys]
        synth = [r for r in self._synthesized.values() if r.key not in known_keys]

        lines.append("<b>Equipo base:</b>")
        for r in known:
            lines.append(f"  {r.emoji} <b>{r.title}</b> → <code>{r.brain}</code>")

        if synth:
            lines.append("")
            lines.append("<b>Agentes sintetizados en sesión:</b>")
            for r in synth:
                skills_str = ", ".join(r.skills[:3])
                lines.append(
                    f"  {r.emoji} <b>{r.title}</b> → <code>{r.brain}</code> "
                    f"<i>[{skills_str}]</i>"
                )

        lines.append("")
        lines.append(
            f"<i>Matryoshka depth: {MAX_DEPTH} | "
            f"Roles disponibles: {len(self._synthesized)}</i>"
        )
        return "\n".join(lines)


# ── Singleton ─────────────────────────────────────────────────────────────────

_factory: Optional[AgentFactory] = None


def get_factory(brain_router: Any = None) -> Optional[AgentFactory]:
    global _factory
    if _factory is None and brain_router is not None:
        _factory = AgentFactory(brain_router)
    return _factory
