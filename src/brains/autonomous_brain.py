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
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

# Set up session logging for autonomous brain activities
_log_path = Path.home() / ".aura" / "memory" / "autonomous_brain_log"
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=_log_path,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin")
_DEFAULT_TIMEOUT = 300  # 5 min — routines can be slow


_SYSTEM_PROMPT = """Eres AURA, la IA personal de Ricardo Pinto corriendo en su Mac.

Tu rol: EJECUTOR AUTÓNOMO e INTELIGENCIA CONTINUA. Cuando recibes una tarea, la completas con las tools disponibles.
No preguntas, no explicas antes de actuar, no pides confirmación.

CONTEXTO Y CONTINUIDAD:
- Siempre consulta el rastro reciente (~/.aura/memory/trace.jsonl) y tu memoria (mcp__aura__memory_search) para "recordar" qué estabas haciendo y no repetir errores.
- Si no sabes algo, INVESTIGA usando bash_run con `gemini -p "tu búsqueda"` o leyendo archivos del proyecto.
- Tus aprendizajes deben ser persistentes: guarda lo que aprendas en ~/.aura/memory/aprendizajes.md.

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
1. Si la tarea dice "envía un correo" → llama send_email con to/subject/body correctos.
2. Si la tarea dice "ejecuta X" → llama bash_run.
3. Si necesitas contexto → usa file_read o memory_search.
4. Siempre confirma el resultado al final (email enviado, archivo creado, etc.).
5. NUNCA uses ANTHROPIC_API_KEY — solo suscripción CLI.
6. Si una tarea es compleja, divídela en pasos y documéntalo en el trace.
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

    def __init__(
        self, model: str = "claude-sonnet-4-5", timeout: int = _DEFAULT_TIMEOUT
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._cli = _find_claude()
        logging.info(
            "AutonomousBrain initialized with model=%s, timeout=%s",
            self._model,
            self._timeout,
        )

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_: Any,
    ) -> BrainResponse:
        if not self._cli:
            logging.error("Claude CLI not found")
            return BrainResponse(
                content="claude CLI not found",
                brain_name=self.name,
                is_error=True,
                error_type="cli_not_found",
            )

        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home() / "claude-code-telegram")
        start = time.time()
        logging.info(
            "Starting autonomous task execution: prompt_length=%d, timeout=%d, cwd=%s",
            len(prompt),
            timeout,
            cwd,
        )

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
            "-p",
            prompt,
            "--model",
            self._model,
            "--output-format",
            "text",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "",  # no plugins → no API key injection
            "--mcp-config",
            _mcp_config,  # solo AURA MCP
            "--strict-mcp-config",  # ignora TODOS los otros MCPs del sistema
            "--append-system-prompt",
            _SYSTEM_PROMPT,
        ]

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # Kill subprocess on timeout OR when outer coroutine is cancelled
            # (asyncio.wait_for on the caller raises CancelledError here)
            if proc is not None:
                try:
                    import signal as _sig

                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            elapsed = int((time.time() - start) * 1000)
            if isinstance(exc, asyncio.TimeoutError):
                logging.warning("Task execution timeout after %d ms", elapsed)
                return BrainResponse(
                    content=f"⏱ Timeout después de {timeout}s",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="timeout",
                )
            # CancelledError — propagate so asyncio task scheduling stays correct
            raise
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            logging.error(
                "Task execution failed with exception: %s", exc, exc_info=True
            )
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
            logging.error(
                "Task execution failed with exit code %d: %s",
                proc.returncode,
                err[:500],
            )
            return BrainResponse(
                content=err[:1000] or f"exit {proc.returncode}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="nonzero_exit",
            )

        logger.info("autonomous_brain_ok", elapsed_ms=elapsed, chars=len(out))
        logging.info(
            "Task execution completed successfully: elapsed_ms=%d, output_chars=%d",
            elapsed,
            len(out),
        )
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

    def generate_strategic_tasks(self) -> list[dict[str, Any]]:
        """Generate strategic tasks from MISSION.md for autonomous development.

        Uses mission_parser to extract uncompleted tasks from MISSION.md,
        ordered by priority tier (Tier 1 first).

        Returns:
            List of task dictionaries ready for execution
        """
        from pathlib import Path
        from src.utils.mission_parser import parse_mission_file

        mission_file = Path.home() / "claude-code-telegram" / "MISSION.md"

        try:
            tasks = parse_mission_file(mission_file)
            logging.info(
                "Strategic tasks generated: count=%d, top_priority=%s",
                len(tasks),
                tasks[0]["title"] if tasks else "none",
            )
            return tasks
        except Exception as e:
            logging.error("Failed to generate strategic tasks: %s", e)
            return []
