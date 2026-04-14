"""AgentSquad — CEO orchestrates the team for complex multi-agent tasks.

Uses AgentFactory for dynamic role creation (matryoshka pattern):
  - Known roles (team.ROLES) used as-is
  - Unknown roles synthesized on the fly with auto system_prompt + optimal brain
  - Each agent can request sub-agents up to factory.MAX_DEPTH levels deep
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable, Optional

import structlog

from .factory import AgentFactory, get_factory
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
    # Opus — only via needs_opus() escalation, not direct subtask routing
    # (too expensive to add as regular subtask)
}


class AgentSquad:
    """Orchestrates a team of AI agents for complex multi-step tasks.

    Simple tasks → normal Cortex routing (not activated).
    Complex tasks → CEO decomposes → factory spawns agents (known + synthesized).
    Each agent can spawn sub-agents via factory matryoshka pattern (depth ≤ 3).
    """

    def __init__(self, brain_router: Any) -> None:
        self._router = brain_router
        self._factory: AgentFactory = get_factory(brain_router) or AgentFactory(brain_router)
        self._conversation: list[AgentMessage] = []
        self._active = False
        logger.info("agent_squad_ready", roles=list(ROLES.keys()), factory="matryoshka")

    # Patterns that justify waking up Opus (Chief Architect)
    _OPUS_TRIGGERS = re.compile(
        r"(?i)\b("
        r"diseña\s+(?:la\s+)?arquitectura|design\s+(?:the\s+)?architecture|"
        r"estrategia\s+(?:completa|de\s+negocio|de\s+producto)|business\s+strategy|"
        r"analiza\s+(?:a\s+fondo|profundamente|en\s+profundidad)|deep\s+analysis|"
        r"investiga\s+(?:todo|a\s+fondo)|research\s+(?:thoroughly|deeply)|"
        r"problema\s+(?:complejo|difícil|imposible)|hard\s+problem|"
        r"toma\s+(?:la\s+)?decisión|make\s+(?:the\s+)?(?:final\s+)?decision|"
        r"razona\s+(?:sobre|acerca)|reason\s+(?:about|through)|"
        r"filosofía|philosophy|innovación|breakthrough|"
        r"mejor\s+enfoque\s+posible|best\s+possible\s+approach|"
        r"piensa\s+(?:bien|profundo|a\s+fondo)|think\s+(?:deeply|carefully|hard)"
        r")\b"
    )

    def needs_opus(self, prompt: str) -> bool:
        """Return True if this task warrants waking up the Chief Architect (Opus)."""
        return bool(self._OPUS_TRIGGERS.search(prompt)) or len(prompt) > 500

    def is_complex_task(self, prompt: str) -> bool:
        """Determine if this task needs multi-agent orchestration.

        Uses meta_router complexity score as primary signal.
        Falls back to heuristic if meta_router unavailable.
        """
        # Primary: meta_router complexity score (already wired in routing)
        try:
            from src.claude.meta_router import route_request, ModelTier
            decision = route_request(prompt)
            # Sonnet-level complexity (score ≥ 5) + multi-domain = squad territory
            if decision.score >= 8:
                return True
        except Exception:
            pass

        # Secondary: explicit multi-step / multi-domain heuristic
        multi_step = re.search(
            r"(?i)\b(y\s+también|también|además|luego|después|primero|segundo|"
            r"paso\s+\d|and\s+then|after\s+that|step\s+\d|pipeline|"
            r"full\s+stack|completo|end.to.end|todo\s+el\s+proceso|workflow)\b",
            prompt,
        )
        # Count distinct domains mentioned
        domains = sum([
            bool(re.search(r"(?i)\b(código|code|implement|build|deploy|api|backend)\b", prompt)),
            bool(re.search(r"(?i)\b(post|contenido|content|social|marketing|copy|campaign)\b", prompt)),
            bool(re.search(r"(?i)\b(diseño|design|ux|ui|imagen|visual|brand)\b", prompt)),
            bool(re.search(r"(?i)\b(investigar|research|analiza|datos|metrics|seo)\b", prompt)),
        ])
        multi_domain = domains >= 2
        long_task = len(prompt) > 200

        return bool(multi_step and long_task) or (multi_domain and long_task)

    def _detect_needed_roles(self, prompt: str) -> list[str]:
        """Detect which roles are needed for this task.

        First checks static _SKILL_ROLE_MAP, then infers from factory
        skill-domain mapping for roles not in catalog.
        """
        needed: set[str] = set()
        prompt_lower = prompt.lower()

        # Static map first (known high-confidence mappings)
        for keyword, role in _SKILL_ROLE_MAP.items():
            if keyword in prompt_lower:
                needed.add(role)
                if role in ("codex_coder", "claude_coder", "qwen_coder"):
                    needed.add("cto")

        # Extended detection: new roles in catalog
        extended_keywords = {
            "copywriter": ["copy", "redacta", "texto", "headline", "eslogan"],
            "designer": ["diseño", "design", "ui", "ux", "visual", "mockup", "wireframe"],
            "researcher": ["investiga", "research", "busca info", "analiza el mercado", "trends", "seo"],
            "devops": ["deploy", "docker", "ci/cd", "pipeline", "infraestructura", "kubernetes"],
            "qa_engineer": ["pruebas", "tests", "testing", "calidad", "bugs", "edge cases"],
            "data_analyst": ["datos", "métricas", "analytics", "dashboard", "kpi", "sql"],
        }
        for role_key, keywords in extended_keywords.items():
            if any(kw in prompt_lower for kw in keywords):
                needed.add(role_key)

        if len(needed) > 1:
            needed.add("coo")

        if not needed:
            needed.add(
                "cto"
                if any(w in prompt_lower for w in ["code", "build", "create", "make", "fix", "crea", "implementa"])
                else "cmo"
            )

        return list(needed)

    async def _call_brain(
        self,
        role_key: str,
        task: str,
        context: str = "",
        timeout: int = 60,
        notify_fn: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Call the brain for a role — uses factory so unknown roles are synthesized."""
        # Get role from factory (creates if not in catalog)
        synth_role = self._factory.get_or_create_role(role_key, task_hint=task)

        brain = self._router.get_brain(synth_role.brain)
        if brain is None:
            brain = self._router.get_brain("qwen-code") or self._router.get_brain("haiku")
        if brain is None:
            return f"[Brain {synth_role.brain} unavailable]"

        context_block = f"[CONTEXT FROM TEAM]\n{context}\n\n" if context else ""
        full_prompt = (
            f"[SYSTEM ROLE: {synth_role.system_prompt}]\n\n"
            f"{context_block}"
            f"[YOUR TASK]\n{task}"
        )

        from src.agents.activity import get_tracker
        tracker = get_tracker()
        tracker.set_working(role_key, task[:80])

        try:
            result = await asyncio.wait_for(
                brain.execute(prompt=full_prompt),
                timeout=timeout,
            )
            content = result.content if hasattr(result, "content") else str(result)
            if not content:
                content = getattr(result, "error_type", None) or "[empty]"
            tracker.set_done(role_key, content[:200])

            # Matryoshka: if agent requests sub-agents, spawn them via factory
            content = await self._resolve_subagent_requests(
                content, context=task, depth=1, notify_fn=notify_fn
            )
            return content
        except asyncio.TimeoutError:
            logger.warning("agent_brain_timeout", role=role_key, brain=synth_role.brain)
            tracker.set_error(role_key, f"timed out after {timeout}s")
            return f"[{synth_role.title} timed out after {timeout}s]"
        except Exception as exc:
            logger.error("agent_brain_error", role=role_key, error=str(exc))
            tracker.set_error(role_key, str(exc))
            return f"[{synth_role.title} error: {exc}]"

    async def _resolve_subagent_requests(
        self,
        content: str,
        context: str,
        depth: int,
        notify_fn: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Detect and resolve __NEEDS__ sub-agent requests (matryoshka)."""
        from .factory import MAX_DEPTH
        if depth >= MAX_DEPTH:
            return content

        pattern = re.compile(
            r"__NEEDS__:\s*\[?([^\]\n→]+)\]?\s*→\s*(.+?)(?=__NEEDS__|$)",
            re.IGNORECASE | re.DOTALL,
        )
        matches = list(pattern.finditer(content))
        if not matches:
            return content

        for match in matches:
            sub_role = match.group(1).strip()
            sub_task = match.group(2).strip()[:400]

            if notify_fn:
                try:
                    await notify_fn(f"  ↳ Invocando <b>{sub_role}</b> (L{depth})...")
                except Exception:
                    pass

            sub_agent = self._factory.spawn(
                role_name=sub_role,
                task_hint=sub_task,
                depth=depth,
                notify_fn=notify_fn,
            )
            sub_result = await sub_agent.execute(sub_task, context=context)
            sub_content = sub_result.flat_content()
            content = content.replace(match.group(0), "").strip()
            content += f"\n\n[{sub_role.upper()} → resultado]\n{sub_content}"

        return content

    async def run(
        self,
        prompt: str,
        notify_fn: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Execute a multi-agent task. Returns the final synthesized response."""
        self._active = True
        self._conversation.clear()

        from src.agents.activity import get_tracker
        get_tracker().start_run(prompt)

        async def notify(msg: str) -> None:
            if notify_fn:
                try:
                    await notify_fn(msg)
                except Exception:
                    pass

        await notify("🏛️ <b>CEO</b> analizando tarea...")

        # Step 1: CEO decomposes the task
        # CEO knows about all available roles including synthesized ones
        known_roles = list(self._factory._synthesized.keys())
        roles_hint = ", ".join(known_roles[:12]) + (
            " (+ cualquier otro especialista que necesites)" if len(known_roles) > 12 else
            " (puedes inventar cualquier rol especialista que necesites)"
        )
        ceo_decompose = await self._call_brain(
            "ceo",
            f"""Analiza esta tarea y crea un plan de trabajo en JSON.
TAREA: {prompt}

Roles disponibles en el equipo: {roles_hint}
IMPORTANTE: Si la tarea requiere un especialista que no está en la lista, INVÉNTALO.
Por ejemplo: "copywriter", "seo_specialist", "ui_designer", "data_analyst", etc.
El sistema creará el agente automáticamente con el modelo correcto.

Responde SOLO con JSON válido:
{{
  "plan_summary": "resumen del plan en 1 frase",
  "subtasks": [
    {{"id": "1", "role": "nombre_del_rol", "task": "descripción específica y concreta", "depends_on": []}}
  ],
  "expected_output": "qué entregamos al final"
}}""",
            timeout=45,
            notify_fn=notify_fn,
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

        _tracker = get_tracker()
        for subtask in subtasks:
            _tracker.add_message("ceo", subtask["role"], subtask["task"][:80], "task")

        # Step 1b: Opus escalation — Chief Architect weighs in on hard problems
        opus_insight = ""
        if self.needs_opus(prompt):
            await notify("🧠 <b>Chief Architect</b> (Opus) — problema complejo detectado, pensando profundo...")
            opus_insight = await self._call_brain(
                "chief_architect",
                f"""El CEO necesita tu perspectiva para una tarea de alta complejidad.

TAREA: {prompt}

PLAN DEL CEO: {plan_summary}

Tu rol: aportar perspectiva estratégica profunda, identificar riesgos ocultos,
señalar el mejor enfoque posible, y enriquecer el plan con tu razonamiento.
Sé conciso pero profundo. Esto guiará al resto del equipo.""",
                timeout=120,  # Opus gets more time — it earns it
            )
            self._conversation.append(
                AgentMessage("chief_architect", "ceo", "review", opus_insight)
            )
            _tracker.add_message("chief_architect", "ceo", opus_insight[:120], "review")
            await notify(
                f"🧠 <b>Chief Architect</b>: {opus_insight[:120]}..."
            )

        agents_preview = ", ".join(s["role"].upper() for s in subtasks)
        await notify(
            f"📋 <b>Plan</b>: {plan_summary}\n👥 Agentes: {agents_preview}"
            + ("\n🧠 Opus guía el equipo" if opus_insight else "")
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
                self._call_brain(
                    s["role"],
                    s["task"],
                    context=f"[Chief Architect guidance]\n{opus_insight}" if opus_insight else "",
                    timeout=150,
                )
                for s in no_deps
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
                await notify(f"✅ <b>{role_title}</b> completó su tarea")

        for subtask in has_deps:
            role = subtask["role"]
            role_title = ROLES[role].title if role in ROLES else role.upper()

            dep_context = "\n\n".join(
                f"[{dep_id}: {results.get(dep_id, 'no result')}]"
                for dep_id in subtask.get("depends_on", [])
            )

            await notify(f"🔄 <b>{role_title}</b> trabajando...")
            result_text = await self._call_brain(
                role, subtask["task"], context=dep_context, timeout=150
            )
            results[subtask["id"]] = result_text
            self._conversation.append(
                AgentMessage(role, "ceo", "result", result_text)
            )
            await notify(f"✅ <b>{role_title}</b> completó")

        # Step 3: COO verification (if multiple agents worked and COO wasn't a subtask)
        coo_verdict = ""
        assigned_roles = [s["role"] for s in subtasks]
        if len(results) > 1 and "coo" not in assigned_roles:
            await notify("📋 <b>COO</b> verificando calidad...")
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
            _tracker.add_message("coo", "ceo", coo_result[:120], "review")
            await notify(f"{verdict_emoji} <b>COO</b>: {coo_result[:120]}")

        # Step 4: CEO synthesizes final response
        await notify("🏛️ <b>CEO</b> sintetizando resultado final...")
        all_work = "\n\n".join(
            f"[{ROLES[s['role']].title if s['role'] in ROLES else s['role'].upper()}"
            f" — subtask {s['id']}]\n{results.get(s['id'], 'no output')}"
            for s in subtasks
        )
        coo_block = f"\nVerificación COO: {coo_verdict}" if coo_verdict else ""

        opus_block = f"\nChief Architect insight (Opus):\n{opus_insight}" if opus_insight else ""

        final = await self._call_brain(
            "ceo",
            (
                "Sintetiza el trabajo del equipo en una respuesta final coherente para el usuario.\n\n"
                f"Tarea original: {prompt}\n\n"
                f"{opus_block}\n"
                f"Trabajo del equipo:\n{all_work}\n"
                f"{coo_block}\n\n"
                "Entrega una respuesta directa, bien estructurada. "
                "No menciones el proceso interno a menos que sea relevante.\n"
                f"Lo esperado era: {expected_output}"
            ),
            timeout=120,
        )

        agents_used = list(set(s["role"] for s in subtasks))
        attribution_parts = []
        if opus_insight:
            attribution_parts.append("🧠 Chief Architect")
        attribution_parts += [
            f"{ROLES[r].emoji} {ROLES[r].title}" if r in ROLES else r.upper()
            for r in agents_used
        ]
        attribution = " · ".join(attribution_parts)

        full_result = f"{final}\n\n---\n_Team: {attribution}_"

        # Store full result in tracker BEFORE marking run ended
        get_tracker().end_run(result=full_result)
        self._active = False

        return full_result

    def get_conversation_log(self) -> list[str]:
        """Get formatted conversation log."""
        return [msg.format_log() for msg in self._conversation]

    def team_status(self) -> str:
        """Format team status as HTML for Telegram."""
        lines = ["<b>🏢 AURA Agent Team</b>\n"]

        # Board tier (Opus) at the top
        board = [r for r in ROLES.values() if r.tier.value == "board"]
        for r in board:
            lines.append(f"{r.emoji} <b>{r.title}</b> <code>{r.brain}</code>")
            lines.append(f"   <i>Se activa para problemas de alta complejidad</i>")

        lines.append("")

        # CEO + org chart
        ceo = ROLES.get("ceo")
        if ceo:
            lines.append(f"{ceo.emoji} <b>{ceo.title}</b> <code>{ceo.brain}</code> — {ceo.full_name}")
            direct_reports = [r for r in ROLES.values() if r.reports_to == "ceo"]
            for i, rep in enumerate(direct_reports):
                connector = "└─" if i == len(direct_reports) - 1 else "├─"
                lines.append(f"  {connector} {rep.emoji} <b>{rep.title}</b> <code>{rep.brain}</code>")
                sub_reports = [r for r in ROLES.values() if r.reports_to == rep.key]
                for j, sub in enumerate(sub_reports):
                    sub_conn = "└─" if j == len(sub_reports) - 1 else "├─"
                    lines.append(f"  │   {sub_conn} {sub.emoji} <b>{sub.title}</b> <code>{sub.brain}</code>")

        lines.append(f"\n{'🟢 <b>Activo</b>' if self._active else '⚡ Listo'}")
        lines.append("<i>Usa /team &lt;tarea&gt; para activar el squad</i>")
        return "\n".join(lines)


_squad: Optional[AgentSquad] = None


def get_squad(brain_router: Any = None) -> Optional[AgentSquad]:
    global _squad
    if _squad is None and brain_router is not None:
        _squad = AgentSquad(brain_router)
    return _squad
