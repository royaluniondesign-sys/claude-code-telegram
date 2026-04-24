"""AURA Proactive Loop — Hermes-style ReAct agent (Think → Act → Observe).

Patrón de Hermes/NousResearch: un solo agente capaz (haiku via claude CLI)
en lugar del anterior conductor 3-capas con Ollama 7B.

Por qué: Ollama 7B no puede diagnosticar fiablemente código Python complejo.
El anterior L1→L2→L3 compone errores: si L1 diagnostica mal, L2 codifica lo
incorrecto, L3 hace commit de basura. Un solo haiku con herramientas directas
(Read/Grep/Edit/Bash) es 10× más fiable y consume menos tokens en total.

Ciclo cada 15 minutos:
  1. Health check (bash, gratis): disco, RAM, errores, tests
  2. auto_executor maneja tareas con fix_command (bash directo)
  3. Tareas sin fix_command → ReAct: un solo haiku con contexto + herramientas
  4. Si no hay tareas → ejecutar una rutina fija del schedule
  5. Append a ~/.aura/memory/trace.jsonl (memoria unificada)
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

# ── Configuración ─────────────────────────────────────────────────────────────

_LOOP_INTERVAL = 900          # 15 minutos
_AURA_ROOT     = Path.home() / "claude-code-telegram"
_TRACE_FILE    = Path.home() / ".aura" / "memory" / "trace.jsonl"
_TRACE_MAX     = 100          # entradas máximas en trace (las más recientes)

_DISK_WARN_GB  = 3.0
_DISK_SKIP_GB  = 1.5

# ── Estado en memoria (para dashboard) ───────────────────────────────────────

_proactive_status: dict = {
    "running": False,
    "last_run_at": None,
    "next_run_at": None,
    "last_result": None,
    "last_steps_ok": 0,
    "last_steps_failed": 0,
    "total_runs": 0,
    "total_steps_ok": 0,
    "total_steps_failed": 0,
    "started_at": None,
}

# ── External task interrupt ────────────────────────────────────────────────────

_external_task_active: bool = False
_external_task_ts: float = 0.0
_EXTERNAL_COOLDOWN = 120


def set_external_task_active(active: bool) -> None:
    global _external_task_active, _external_task_ts
    _external_task_active = active
    if active:
        _external_task_ts = time.time()


def is_external_task_active() -> bool:
    if not _external_task_active:
        return False
    return time.time() - _external_task_ts < _EXTERNAL_COOLDOWN


def get_proactive_status() -> dict:
    return {**_proactive_status}


# ── Memoria unificada — append-only trace ────────────────────────────────────

def _trace_append(event: str, data: dict) -> None:
    """Añade una entrada al trace unificado. Trunca a _TRACE_MAX entradas."""
    try:
        _TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(UTC).isoformat(), "event": event, **data}
        lines = []
        if _TRACE_FILE.exists():
            lines = _TRACE_FILE.read_text().splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        # Mantener solo las últimas _TRACE_MAX
        _TRACE_FILE.write_text("\n".join(lines[-_TRACE_MAX:]) + "\n")
    except Exception as exc:
        logger.debug("trace_append_error", error=str(exc))


def _trace_recent(n: int = 10) -> list[dict]:
    """Devuelve las últimas n entradas del trace."""
    try:
        if not _TRACE_FILE.exists():
            return []
        lines = _TRACE_FILE.read_text().splitlines()
        return [json.loads(l) for l in lines[-n:] if l.strip()]
    except Exception:
        return []


# ── Health checks (bash, sin costo de tokens) ─────────────────────────────────

_LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB por log file
_LOG_KEEP_LINES = 5_000            # mantener últimas 5000 líneas tras trim


def _trim_logs() -> None:
    """Recorta logs/*.log a _LOG_KEEP_LINES si superan _LOG_MAX_BYTES."""
    log_dir = _AURA_ROOT / "logs"
    if not log_dir.exists():
        return
    for log_file in log_dir.glob("*.log"):
        try:
            if log_file.stat().st_size > _LOG_MAX_BYTES:
                lines = log_file.read_text(errors="replace").splitlines()
                trimmed = "\n".join(lines[-_LOG_KEEP_LINES:]) + "\n"
                log_file.write_text(trimmed)
                logger.info("log_trimmed", file=log_file.name,
                            before=len(lines), after=_LOG_KEEP_LINES)
        except Exception as exc:
            logger.debug("log_trim_error", file=log_file.name, error=str(exc))


def _free_disk_gb() -> float:
    import shutil
    return shutil.disk_usage("/").free / 1e9


def _recent_errors(n: int = 200) -> list[str]:
    log = _AURA_ROOT / "logs" / "bot.stderr.log"
    if not log.exists():
        return []
    lines = log.read_text(errors="replace").splitlines()[-n:]
    return [l for l in lines if "error" in l.lower() and "warn" not in l.lower()][:10]


def _run_tests() -> tuple[bool, str]:
    """Corre pytest y devuelve (passed, summary)."""
    venv = _AURA_ROOT / ".venv" / "bin" / "pytest"
    if not venv.exists():
        return True, "no pytest"
    r = subprocess.run(
        [str(venv), "tests/", "-q", "--tb=no", "--no-header"],
        capture_output=True, text=True, timeout=60, cwd=str(_AURA_ROOT),
    )
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[:100]
    return r.returncode == 0, last


def _auto_cleanup_disk() -> str:
    r = subprocess.run(
        "docker system prune -f 2>/dev/null; "
        "find ~/claude-code-telegram/logs -name '*.log' -size +10M "
        "-exec truncate -s 1M {} \\; 2>/dev/null; "
        "df -h / | tail -1",
        shell=True, capture_output=True, text=True, timeout=30,
    )
    return r.stdout.strip()[:200]


# ── Rutinas fijas (sin LLM) ───────────────────────────────────────────────────
# Reemplazan el "pídele a Ollama que invente tareas".
# Cada rutina retorna (descripción, tarea_creada_bool).

_ROUTINE_POINTER = 0  # ciclo round-robin entre rutinas
_ROUTINES = ["check_errors", "run_tests", "check_disk", "check_ram", "git_status", "memory_summary"]


def _routine_check_errors() -> tuple[str, bool]:
    errors = _recent_errors()
    if not errors:
        return "No hay errores recurrentes.", False
    from .task_store import create_task, list_tasks
    existing = {t["title"] for t in list_tasks(status="pending")}
    created = 0
    import re
    patterns: dict[str, int] = {}
    for line in errors:
        m = re.search(r'"event"\s*[=:]\s*"([^"]{4,60})"', line)
        if m:
            patterns[m.group(1)] = patterns.get(m.group(1), 0) + 1
    for pattern, count in patterns.items():
        if count >= 3:
            title = f"Fix recurring error: {pattern}"
            if title not in existing:
                create_task(title, description=f"{count}× en logs recientes",
                            priority="high" if count >= 8 else "medium",
                            category="fix", auto_fix=False,
                            tags=["auto", "log_error"])
                created += 1
    return f"{len(errors)} errores encontrados, {created} tareas creadas.", created > 0


def _routine_run_tests() -> tuple[str, bool]:
    ok, summary = _run_tests()
    _trace_append("tests", {"ok": ok, "summary": summary})
    if ok:
        return f"Tests OK: {summary}", False
    # Debounce: solo crear tarea si falla 2 veces consecutivas en el trace
    recent = _trace_recent(10)
    test_results = [e for e in recent if e.get("event") == "tests"]
    consecutive_fails = 0
    for e in reversed(test_results):
        if not e.get("ok"):
            consecutive_fails += 1
        else:
            break
    if consecutive_fails >= 2:
        from .task_store import create_task, list_tasks
        title = "Fix failing tests"
        existing = {t["title"] for t in list_tasks(status="pending")}
        if title not in existing:
            create_task(title, description=summary, priority="high",
                        category="fix", auto_fix=False, tags=["auto", "tests"])
    # Nunca notificar por Telegram — los tests flaky no son emergencia
    return f"Tests: {summary}", False


def _routine_check_disk() -> tuple[str, bool]:
    free = _free_disk_gb()
    if free >= _DISK_WARN_GB:
        return f"Disco OK: {free:.1f}GB libre.", False
    from .task_store import create_task, list_tasks
    title = f"Limpiar disco — solo {free:.1f}GB libre"
    existing = {t["title"] for t in list_tasks(status="pending")}
    if title not in existing:
        create_task(title, priority="critical" if free < 2 else "high",
                    category="maintenance", auto_fix=True,
                    fix_command="docker system prune -f; df -h /",
                    tags=["auto", "disk"])
    return f"Disco bajo: {free:.1f}GB", True


def _routine_check_ram() -> tuple[str, bool]:
    """Ejecuta cleanup de RAM si está >95%, reporta si >90%."""
    cleanup_script = Path.home() / ".aura" / "scripts" / "cleanup_ram.sh"
    try:
        r = subprocess.run(
            [str(cleanup_script)],
            capture_output=True, text=True, timeout=60,
        )
        output = r.stdout.strip()
        # Parse: "RAM usado: XX%"
        ram_pct = 0
        for line in output.split("\n"):
            if "RAM usado:" in line:
                ram_pct = int(line.split(":")[-1].strip().rstrip("%"))
                break

        _trace_append("ram_check", {"ram_pct": ram_pct, "output": output[:200]})

        if ram_pct >= 90:
            logger.warning("high_ram_usage", ram_pct=ram_pct)

        return f"RAM: {ram_pct}% usado", False
    except Exception as exc:
        logger.warning("ram_check_error", error=str(exc))
        return f"RAM check error: {exc}", False


def _routine_git_status() -> tuple[str, bool]:
    r = subprocess.run(
        ["git", "-C", str(_AURA_ROOT), "status", "--short"],
        capture_output=True, text=True, timeout=5,
    )
    status = r.stdout.strip()
    _trace_append("git_status", {"status": status[:200]})
    return f"Git: {status[:100] or 'clean'}", False


def _routine_memory_summary() -> tuple[str, bool]:
    recent = _trace_recent(5)
    summary = f"{len(recent)} eventos recientes en trace. Último: {recent[-1].get('event','?') if recent else 'ninguno'}"
    _trace_append("memory_summary", {"recent_count": len(recent)})
    return summary, False


_ROUTINE_FNS = {
    "check_errors":   _routine_check_errors,
    "run_tests":      _routine_run_tests,
    "check_disk":     _routine_check_disk,
    "check_ram":      _routine_check_ram,
    "git_status":     _routine_git_status,
    "memory_summary": _routine_memory_summary,
}


def _run_next_routine() -> tuple[str, bool]:
    global _ROUTINE_POINTER
    name = _ROUTINES[_ROUTINE_POINTER % len(_ROUTINES)]
    _ROUTINE_POINTER += 1
    fn = _ROUTINE_FNS[name]
    try:
        result, created = fn()
        logger.info("routine_ok", name=name, result=result[:80])
        return f"[{name}] {result}", created
    except Exception as exc:
        logger.warning("routine_error", name=name, error=str(exc))
        return f"[{name}] error: {exc}", False


# ── ReAct: ejecutar tarea sin fix_command con un solo haiku ──────────────────

async def _react_execute_task(task: dict, brain_router: Any) -> tuple[bool, str]:
    """Hermes-style ReAct: un solo haiku con herramientas para resolver la tarea.

    Integra RAG para inyectar conocimiento previo del sistema y de aprendizajes pasados.
    """
    title = task.get("title", "")
    desc  = task.get("description", "")
    tid   = task.get("id", "")

    # ── RAG: buscar conocimiento previo ──────────────────────────────────────
    rag_ctx = ""
    try:
        from ..rag.retriever import RAGRetriever
        retriever = RAGRetriever()
        # Buscamos en memoria y código sobre el tema de la tarea
        results = await retriever.search(f"{title} {desc}", limit=5)
        if results:
            rag_ctx = "\n".join(f"- [{r.get('source_type')}] {r.get('content')[:300]}..." for r in results)
    except Exception as exc:
        logger.debug("proactive_rag_search_fail", error=str(exc))

    # Contexto: últimas entradas del trace + errores recientes
    recent = _trace_recent(5)
    trace_ctx = "\n".join(f"- [{e.get('event')}] {json.dumps(e)[:80]}" for e in recent)
    errors = _recent_errors(50)
    error_ctx = "\n".join(errors[:5]) if errors else "ninguno"

    prompt = f"""Eres el agente autónomo de AURA. Resuelve esta tarea concreta:

TAREA: {title}
DETALLE: {desc}

CONOCIMIENTO PREVIO (RAG):
{rag_ctx or 'sin coincidencias en memoria'}

CONTEXTO RECIENTE (trace):
{trace_ctx or 'sin entradas previas'}

ERRORES RECIENTES:
{error_ctx}

REGLAS:
- Usa el CONOCIMIENTO PREVIO para no repetir errores y entender la arquitectura.
- Lee los archivos relevantes antes de editar.
- Haz el cambio mínimo que resuelve el problema.
- Verifica sintaxis: python3 -c "import ast; ast.parse(open('archivo').read())"
- Si hay tests, ejecuta: .venv/bin/pytest tests/ -q --tb=short -x
- Si todo OK, haz commit: git add -p && git commit -m "fix: {title[:50]}"
- Si el problema requiere información que no tienes, PARA y responde con "BLOCKED: motivo"
- NO inventes soluciones si no puedes verificarlas.

Raíz del proyecto: {_AURA_ROOT}
"""

    brain = brain_router.get_brain("haiku") if brain_router else None
    if not brain:
        brain = brain_router.get_brain("sonnet") if brain_router else None
    if not brain:
        return False, "no brain available"

    # Tool gating: proactive agent solo necesita leer/editar/bash(git)
    _REACT_TOOLS = ["Read", "Grep", "Edit", "Write", "Bash"]

    try:
        resp = await brain.execute(
            prompt,
            working_directory=str(_AURA_ROOT),
            timeout_seconds=180,
            allowed_tools=_REACT_TOOLS,
        )
        success = not resp.is_error and "BLOCKED:" not in (resp.content or "")
        result = (resp.content or "")[:300]

        _trace_append("react_task", {
            "task_id": tid[:8], "title": title[:60],
            "success": success, "result": result,
        })
        return success, result
    except Exception as exc:
        logger.error("react_execute_error", error=str(exc))
        return False, str(exc)[:200]


# ── Ciclo principal ────────────────────────────────────────────────────────────

async def run_self_improvement(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
    source: str = "proactive",
) -> Optional[str]:
    """Un ciclo completo del agente. Retorna resumen o None (silencioso)."""
    global _proactive_status

    if is_external_task_active():
        logger.info("proactive_skip_external_task_active")
        return None

    _proactive_status["running"] = True
    _proactive_status["last_run_at"] = datetime.now(UTC).isoformat()
    _proactive_status["total_runs"] = _proactive_status.get("total_runs", 0) + 1

    steps_ok, steps_fail = 0, 0
    notify_parts: list[str] = []

    try:
        # ── 0. Log rotation (gratis, siempre) ────────────────────────────────
        _trim_logs()

        # ── 1. Disco ──────────────────────────────────────────────────────────
        free_gb = _free_disk_gb()
        if free_gb < _DISK_WARN_GB:
            msg = _auto_cleanup_disk()
            logger.warning("disk_low_cleaned", free_gb=round(free_gb, 1))
            notify_parts.append(f"💾 Disco bajo ({free_gb:.1f}GB) — limpiado")

        # ── 2. Auto-executor: tareas con fix_command (bash, sin tokens) ───────
        try:
            from .auto_executor import run_pending_tasks
            processed = await run_pending_tasks(notify=notify_fn)
            if processed:
                steps_ok += processed
                logger.info("proactive_auto_exec_done", processed=processed)
        except Exception as exc:
            logger.warning("proactive_auto_exec_error", error=str(exc))

        # ── 3. ReAct: una tarea de conductor si hay pendientes ─────────────────
        task_executed = False
        task_to_cleanup: Optional[dict] = None
        try:
            from .task_store import list_tasks, update_task, complete_task, fail_task
            # Tareas sin fix_command que necesitan razonamiento
            pending = [
                t for t in list_tasks(status="pending")
                if t.get("auto_fix") and not (t.get("fix_command") or "").strip()
            ]
            if pending:
                task = sorted(
                    pending,
                    key=lambda t: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                        t.get("priority", "medium"), 2
                    ),
                )[0]
                logger.info("react_task_start", title=task["title"][:60])
                update_task(task["id"], status="in_progress",
                            attempts=(task.get("attempts") or 0) + 1)
                ok, result = await _react_execute_task(task, brain_router)
                if ok:
                    complete_task(task["id"], result)
                    steps_ok += 1
                    notify_parts.append(f"✅ {task['title'][:60]}")
                else:
                    attempts = (task.get("attempts") or 0) + 1
                    if attempts >= 3:
                        fail_task(task["id"], result)
                        notify_parts.append(f"❌ Abandonado (3×): {task['title'][:50]}")
                    else:
                        update_task(task["id"], status="pending", result=result)
                    steps_fail += 1
                task_executed = True
        except Exception as exc:
            logger.warning("react_cycle_error", error=str(exc))
            steps_fail += 1

        # ── 4. Si no hubo tareas → rutina fija (sin tokens) ───────────────────
        if not task_executed:
            try:
                summary, created = _run_next_routine()
                if created:
                    notify_parts.append(f"🔍 {summary[:80]}")
                steps_ok += 1
            except Exception as exc:
                logger.warning("routine_cycle_error", error=str(exc))

    finally:
        _proactive_status["running"] = False
        _proactive_status["last_steps_ok"] = steps_ok
        _proactive_status["last_steps_failed"] = steps_fail
        _proactive_status["total_steps_ok"] = (
            _proactive_status.get("total_steps_ok", 0) + steps_ok
        )
        _proactive_status["total_steps_failed"] = (
            _proactive_status.get("total_steps_failed", 0) + steps_fail
        )
        result_str = f"ok={steps_ok} fail={steps_fail}"
        _proactive_status["last_result"] = result_str
        _trace_append("cycle", {"source": source, "ok": steps_ok, "fail": steps_fail})

    if notify_parts:
        return "\n".join(notify_parts)
    return None


# ── Entrypoints públicos ───────────────────────────────────────────────────────

async def run_proactive_cycle(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
) -> str:
    summary = await run_self_improvement(brain_router, notify_fn=notify_fn, source="scheduler")
    return summary or ""


async def start_proactive_loop(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
) -> None:
    """Loop de fondo. Se llama una vez al arrancar el bot."""
    logger.info("proactive_loop_started", interval_min=_LOOP_INTERVAL // 60)
    await asyncio.sleep(60)  # dejar que el bot arranque primero

    while True:
        free_gb = _free_disk_gb()
        if free_gb < _DISK_SKIP_GB:
            logger.error("disk_critical_skip_proactive", free_gb=round(free_gb, 1))
            _proactive_status["last_result"] = f"skipped: disk {free_gb:.1f}GB"
            await asyncio.sleep(_LOOP_INTERVAL)
            continue

        try:
            summary = await asyncio.wait_for(
                run_self_improvement(brain_router, notify_fn=notify_fn, source="proactive"),
                timeout=300,
            )
            if summary and notify_fn:
                try:
                    await notify_fn(summary)
                except Exception:
                    pass
        except asyncio.TimeoutError:
            logger.error("proactive_loop_timeout", timeout_s=300)
            _proactive_status["last_result"] = "timeout"
        except asyncio.CancelledError:
            logger.info("proactive_loop_cancelled")
            return
        except Exception as exc:
            logger.error("proactive_loop_exception", error=str(exc))
            _proactive_status["last_result"] = "exception"

        next_ts = datetime.fromtimestamp(time.time() + _LOOP_INTERVAL, tz=UTC).isoformat()
        _proactive_status["next_run_at"] = next_ts
        await asyncio.sleep(_LOOP_INTERVAL)
