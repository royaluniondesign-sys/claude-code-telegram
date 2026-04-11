"""AgentSquad — CEO orchestrates the team for complex multi-agent tasks."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable, Optional

import structlog

from .team import ROLES, AgentMessage, AgentRole

logger = structlog.get_logger()

# Skills → role mapping for delegation
_SKILL_ROLE_MAP: dict[str, str] = {
    "code": "cto",
    "coding": "cto",
    "architecture": "cto",
    "review": "cto",
    "implement": "claude_coder",
    "debug": "claude_coder",
    "test": "claude_coder",
    "scaffold": "codex_coder",
    "generate": "codex_coder",
    "boilerplate": "codex_coder",
    "content": "cmo",
    "post": "cmo",
    "social": "cmo",
    "marketing": "cmo",
    "copy": "cmo",
    "campaign": "cmo",
    "instagram": "cmo",
    "twitter": "cmo",
    "linkedin": "cmo",
    "verify": "coo",
    "check": "coo",
    "quality": "coo",
    "operations": "coo",
}


class AgentSquad:
    """Orchestrates a team of AI agents for complex multi-step tasks.

    Simple tasks still go through normal Cortex routing.
    Complex tasks (multi-domain, multi-step) get decomposed and delegated.
    """

    def __init__(self, brain_router: Any) -> None:
        self._router = brain_router
        self._conversation: list[AgentMessage] = []
        self._active = False
        logger.info("agent_squad_ready", roles=list(ROLES.keys()))

    def is_complex_task(self, prompt: str) -> bool:
        """Heuristic: is this task complex enough for multi-agent?"""
        multi_step = re.search(
            r"(?i)\b(y\s+también|también|además|luego|después|primero|segundo|"
            r"and\s+then|then|after\s+that|step\s+\d|multi|pipeline|full\s+stack|"
            r"completo|complete|end.to.end|todo\s+el\s+proceso|todo\s+el\s+flujo)\b",
            prompt,
        )
        code_words = bool(
            re.search(
                r"(?i)\b(código|code|implement|build|create.*app|deploy)\b", prompt
            )
        )
        content_words = bool(
            re.search(
                r"(?i)\b(post|contenido|content|social|marketing|email|copy)\b", prompt
            )
        )
        multi_domain = code_words and content_words
        long_task = len(prompt) > 150

        return bool(multi_step or multi_domain) and long_task

    def _detect_needed_roles(self, prompt: str) -> list[str]:
        """Detect which roles are needed for this task."""
        needed: set[str] = set()
        prompt_lower = prompt.lower()

        for keyword, role in _SKILL_ROLE_MAP.items():
            if keyword in prompt_lower:
                needed.add(role)
                if role in ("codex_coder", "claude_coder"):
                    needed.add("cto")

        if len(needed) > 1:
            needed.add("coo")

        if not needed:
            needed.add(
                "cto"
                if any(
                    w in prompt_lower for w in ["code", "build", "create", "make", "fix"]
                )
                else "cmo"
            )

        return list(needed)

    async def _call_brain(
        self,
        role_key: str,
        task: str,
        context: str = "",
        timeout: int = 60,
    ) -> str:
        """Call the brain for a specific role with its persona."""
        role = ROLES.get(role_key)
        if not role:
            return f"[Role {role_key} not found]"

        brain = self._router.get_brain(role.brain)
        if brain is None:
            brain = self._router.get_brain("haiku")
        if brain is None:
            return f"[Brain {role.brain} unavailable]"

        context_block = (
            f"[CONTEXT FROM TEAM]\n{context}\n\n" if context else ""
        )
        full_prompt = (
            f"[SYSTEM ROLE: {role.system_prompt}]\n\n"
            f"{context_block}"
            f"[YOUR TASK]\n{task}"
        )

        try:
            result = await asyncio.wait_for(
                brain.execute(prompt=full_prompt),
                timeout=timeout,
            )
            if hasattr(result, "content"):
                return result.content or (result.error_type or "[empty]")
            return str(result)
        except asyncio.TimeoutError:
            logger.warning(
                "agent_brain_timeout", role=role_key, brain=role.brain, timeout=timeout
            )
            return f"[{role.title} timed out after {timeout}s]"
        except Exception as exc:
            logger.error(
                "agent_brain_error", role=role_key, brain=role.brain, error=str(exc)
            )
            return f"[{role.title} error: {exc}]"

    async def run(
        self,
        prompt: str,
        notify_fn: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Execute a multi-agent task. Returns the final synthesized response."""
        self._active = True
        self._conversation.clear()

        async def notify(msg: str) -> None:
            if notify_fn:
                try:
                    await notify_fn(msg)
                except Exception:
                    pass

        await notify("🏛️ **CEO** analizando tarea...")

        # Step 1: CEO decomposes the task
        ceo_decompose = await self._call_brain(
            "ceo",
            f"""Analiza esta tarea y crea un plan de trabajo en JSON:
TAREA: {prompt}

Responde SOLO con JSON válido:
{{
  "plan_summary": "resumen del plan en 1 frase",
  "subtasks": [
    {{"id": "1", "role": "cto|cmo|codex_coder|claude_coder|coo", "task": "descripción específica", "depends_on": []}}
  ],
  "expected_output": "qué entregamos al final"
}}""",
            timeout=45,
        )

        subtasks: list[dict] = []
        plan_summary = "Ejecutando plan multi-agente"
        expected_output = ""

        try:
            json_match = re.search(r"\{[\s\S]*\}", ceo_decompose)
            if json_match:
                plan = json.loads(json_match.group())
                subtasks = plan.get("subtasks", [])
                plan_summary = plan.get("plan_summary", plan_summary)
                expected_output = plan.get("expected_output", "")
        except Exception as parse_err:
            logger.warning("ceo_plan_parse_failed", error=str(parse_err))

        if not subtasks:
            needed_roles = self._detect_needed_roles(prompt)
            subtasks = [
                {"id": str(i + 1), "role": r, "task": prompt, "depends_on": []}
                for i, r in enumerate(needed_roles)
            ]

        self._conversation.append(AgentMessage("ceo", "team", "task", plan_summary))
        agents_preview = ", ".join(
            s["role"].upper() for s in subtasks
        )
        await notify(
            f"📋 **Plan**: {plan_summary}\n👥 Agentes: {agents_preview}"
        )

        # Step 2: Execute subtasks (parallel where no dependencies)
        results: dict[str, str] = {}

        no_deps = [s for s in subtasks if not s.get("depends_on")]
        has_deps = [s for s in subtasks if s.get("depends_on")]

        if no_deps:
            role_names = [
                ROLES[s["role"]].title if s["role"] in ROLES else s["role"].upper()
                for s in no_deps
            ]
            await notify(f"⚡ Ejecutando en paralelo: {', '.join(role_names)}...")

            coros = [
                self._call_brain(s["role"], s["task"], timeout=90) for s in no_deps
            ]
            outputs = await asyncio.gather(*coros, return_exceptions=True)

            for subtask, output in zip(no_deps, outputs):
                role = subtask["role"]
                result_text = (
                    str(output)
                    if not isinstance(output, Exception)
                    else f"Error: {output}"
                )
                results[subtask["id"]] = result_text
                self._conversation.append(
                    AgentMessage(role, "ceo", "result", result_text)
                )
                role_title = ROLES[role].title if role in ROLES else role.upper()
                await notify(f"✅ **{role_title}** completó su tarea")

        for subtask in has_deps:
            role = subtask["role"]
            role_title = ROLES[role].title if role in ROLES else role.upper()

            dep_context = "\n\n".join(
                f"[{dep_id}: {results.get(dep_id, 'no result')}]"
                for dep_id in subtask.get("depends_on", [])
            )

            await notify(f"🔄 **{role_title}** trabajando...")
            result_text = await self._call_brain(
                role, subtask["task"], context=dep_context, timeout=90
            )
            results[subtask["id"]] = result_text
            self._conversation.append(
                AgentMessage(role, "ceo", "result", result_text)
            )
            await notify(f"✅ **{role_title}** completó")

        # Step 3: COO verification (if multiple agents worked and COO wasn't a subtask)
        coo_verdict = ""
        assigned_roles = [s["role"] for s in subtasks]
        if len(results) > 1 and "coo" not in assigned_roles:
            await notify("📋 **COO** verificando calidad...")
            all_results = "\n\n".join(
                f"[{tid}]: {r}" for tid, r in results.items()
            )
            coo_result = await self._call_brain(
                "coo",
                (
                    f"Verifica la calidad del trabajo del equipo. "
                    f"Tarea original: {prompt}\n\nResultados:\n{all_results}\n\n"
                    "Responde: APROBADO o RECHAZADO con una nota corta."
                ),
                timeout=45,
            )
            coo_verdict = coo_result
            verdict_emoji = "✅" if "APROBADO" in coo_result.upper() else "⚠️"
            self._conversation.append(
                AgentMessage("coo", "ceo", "verify", coo_result)
            )
            await notify(f"{verdict_emoji} **COO**: {coo_result[:120]}")

        # Step 4: CEO synthesizes final response
        await notify("🏛️ **CEO** sintetizando resultado final...")
        all_work = "\n\n".join(
            f"[{ROLES[s['role']].title if s['role'] in ROLES else s['role'].upper()}"
            f" — subtask {s['id']}]\n{results.get(s['id'], 'no output')}"
            for s in subtasks
        )
        coo_block = f"\nVerificación COO: {coo_verdict}" if coo_verdict else ""

        final = await self._call_brain(
            "ceo",
            (
                "Sintetiza el trabajo del equipo en una respuesta final coherente para el usuario.\n\n"
                f"Tarea original: {prompt}\n\n"
                f"Trabajo del equipo:\n{all_work}\n"
                f"{coo_block}\n\n"
                "Entrega una respuesta directa, bien estructurada. "
                "No menciones el proceso interno a menos que sea relevante.\n"
                f"Lo esperado era: {expected_output}"
            ),
            timeout=60,
        )

        self._active = False

        agents_used = list(set(s["role"] for s in subtasks))
        attribution = " · ".join(
            f"{ROLES[r].emoji} {ROLES[r].title}" if r in ROLES else r.upper()
            for r in agents_used
        )

        return f"{final}\n\n---\n_Team: {attribution}_"

    def get_conversation_log(self) -> list[str]:
        """Get formatted conversation log."""
        return [msg.format_log() for msg in self._conversation]

    def team_status(self) -> str:
        """Format team status for /team command."""
        lines = ["**🏢 AURA Agent Team**\n"]
        top_roles = [
            r for r in ROLES.values()
            if r.tier.value == "executive" and r.reports_to is None
        ]
        for top in top_roles:
            lines.append(f"{top.emoji} **{top.title}** — {top.full_name}")
            direct_reports = [r for r in ROLES.values() if r.reports_to == top.key]
            for rep in direct_reports:
                lines.append(f"  ├─ {rep.emoji} **{rep.title}** ({rep.brain})")
                sub_reports = [r for r in ROLES.values() if r.reports_to == rep.key]
                for sub in sub_reports:
                    lines.append(f"  │   └─ {sub.emoji} **{sub.title}** ({sub.brain})")
        lines.append(f"\n{'🟢 Active' if self._active else '⚡ Ready'}")
        return "\n".join(lines)


_squad: Optional[AgentSquad] = None


def get_squad(brain_router: Any = None) -> Optional[AgentSquad]:
    global _squad
    if _squad is None and brain_router is not None:
        _squad = AgentSquad(brain_router)
    return _squad
