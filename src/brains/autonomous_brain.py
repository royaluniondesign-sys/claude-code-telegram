"""AutonomousBrain — Claude con acceso completo a AURA MCP tools.

Este brain es el núcleo inteligente para rutinas y tareas autónomas.
A diferencia de ClaudeBrain, NO usa --setting-sources "" por lo que
claude carga sus settings normales y tiene acceso a:
  - mcp__aura__send_email
  - mcp__aura__bash_run
  - mcp__aura__file_read / file_write
  - mcp__aura__memory_search / memory_store
  - mcp__aura__get_aura_status
  - mcp__aura__git_commit / git_log / git_status
  - mcp__aura__get_terminal_url

El usuario escribe lenguaje natural → AURA elige la tool correcta → acción real.
No hay casos especiales. La inteligencia es del modelo, no del código.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin")
_DEFAULT_TIMEOUT = 300  # 5 min — routines can be slow


_SYSTEM_PROMPT = """Eres AURA, la IA personal de Ricardo Pinto corriendo en su Mac.

Tu rol: EJECUTOR AUTÓNOMO. Cuando recibes una tarea, la completas con las tools disponibles.
No preguntas, no explicas antes de actuar, no pides confirmación.

TOOLS DISPONIBLES (úsalas directamente):
- mcp__aura__send_email — enviar correos vía Resend
- mcp__aura__bash_run — ejecutar cualquier comando shell
- mcp__aura__file_read — leer archivos
- mcp__aura__file_write — escribir/crear archivos
- mcp__aura__memory_search — buscar en memoria persistente de AURA
- mcp__aura__memory_store — guardar información en memoria
- mcp__aura__get_aura_status — estado del sistema
- mcp__aura__git_commit / git_log / git_status — operaciones git
- mcp__aura__get_terminal_url — URL de Termora para acceso remoto

REGLAS:
1. Si la tarea dice "envía un correo" → llama send_email con to/subject/body correctos
2. Si la tarea dice "ejecuta X" → llama bash_run
3. Si necesitas leer contexto primero → usa file_read o memory_search, luego actúa
4. Siempre confirma el resultado al final (email enviado, archivo creado, etc.)
5. NUNCA uses ANTHROPIC_API_KEY — solo suscripción CLI
"""


def _find_claude() -> str | None:
    for p in [
        shutil.which("claude"),
        str(Path.home() / ".local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]:
        if p and Path(p).exists():
            return p
    return None


class AutonomousBrain(Brain):
    """Claude con acceso total a AURA MCP tools. El brain para ejecución autónoma."""

    name = "autonomous"
    display_name = "AURA Autónomo"
    emoji = "🤖"

    def __init__(self, model: str = "claude-sonnet-4-5", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._model = model
        self._timeout = timeout
        self._cli = _find_claude()

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_: Any,
    ) -> BrainResponse:
        if not self._cli:
            return BrainResponse(
                content="claude CLI not found",
                brain_name=self.name,
                is_error=True,
                error_type="cli_not_found",
            )

        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home() / "claude-code-telegram")
        start = time.time()

        # Clear ANTHROPIC_API_KEY — solo suscripción, nunca cargos por token
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        env["PATH"] = f"{_EXTRA_PATH}:{env.get('PATH', '')}"

        # --setting-sources ""  → no carga plugins de usuario (previene API key injection)
        # --strict-mcp-config   → SOLO carga el MCP de AURA (no computer-use, no otros)
        # Esto elimina los dialogs de permiso macOS que disparan computer-use MCP o codex.
        _mcp_config = str(
            Path(__file__).parent.parent.parent / "config" / "aura-mcp-only.json"
        )
        cmd = [
            self._cli,
            "-p", prompt,
            "--model", self._model,
            "--output-format", "text",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--setting-sources", "",           # no plugins → no API key injection
            "--mcp-config", _mcp_config,       # solo AURA MCP
            "--strict-mcp-config",             # ignora TODOS los otros MCPs del sistema
            "--append-system-prompt", _SYSTEM_PROMPT,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            elapsed = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"⏱ Timeout después de {timeout}s",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="timeout",
            )
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"❌ Error: {exc}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="exception",
            )

        elapsed = int((time.time() - start) * 1000)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if not out and proc.returncode != 0:
            return BrainResponse(
                content=err[:1000] or f"exit {proc.returncode}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="nonzero_exit",
            )

        logger.info("autonomous_brain_ok", elapsed_ms=elapsed, chars=len(out))
        return BrainResponse(content=out, brain_name=self.name, duration_ms=elapsed)

    async def health_check(self) -> BrainStatus:
        if not self._cli:
            return BrainStatus.NOT_INSTALLED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self._model,
            "cli": self._cli or "not found",
            "tools": "mcp__aura__* (full stack)",
            "cost": "Claude subscription (no API key)",
        }

    def generate_tasks(self):
        from pathlib import Path
        import re

        def parse_mission(file_path):
            with open(file_path, 'r') as file:
                content = file.read()
            # Extract key mission objectives using regular expressions
            objectives = re.findall(r'## (.+?)\n', content)
            return objectives

        mission_file_path = Path('/Users/oxyzen/claude-code-telegram/MISISON.md')
        objectives = parse_mission(mission_file_path)

        # Prioritize tasks based on the objectives
        tasks = [f"Improve {objective}" for objective in objectives]
        return tasks
