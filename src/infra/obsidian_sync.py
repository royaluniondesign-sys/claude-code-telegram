"""Obsidian Sync — mantiene el vault de Obsidian actualizado con la memoria de AURA.

Sincroniza archivos clave de ~/.aura/memory/ → ~/Obsidian/
Corre automáticamente cada hora + en startup.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()

_MEMORY   = Path.home() / ".aura" / "memory"
_OBSIDIAN = Path.home() / "Obsidian"

# Archivos a sincronizar: (origen, destino_en_obsidian)
_SYNC_MAP = [
    # Shared cross-agent
    (_MEMORY / "shared" / "tasks.md",        _OBSIDIAN / "shared_tasks.md"),
    (_MEMORY / "shared" / "projects.md",     _OBSIDIAN / "shared_projects.md"),
    (_MEMORY / "shared" / "capabilities.md", _OBSIDIAN / "capabilities.md"),
    (_MEMORY / "shared" / "learnings.md",    _OBSIDIAN / "shared_learnings.md"),
    # AURA core memory
    (_MEMORY / "MEMORY.md",                  _OBSIDIAN / "AURA_MEMORY.md"),
    (_MEMORY / "self-awareness.md",          _OBSIDIAN / "AURA_self_awareness.md"),
    (_MEMORY / "services.md",                _OBSIDIAN / "AURA_services.md"),
    (_MEMORY / "session-plan.md",            _OBSIDIAN / "AURA_session_plan.md"),
    (_MEMORY / "social-roadmap.md",          _OBSIDIAN / "social_roadmap.md"),
    (_MEMORY / "hermes.md",                  _OBSIDIAN / "hermes.md"),
    # Mesh log
    (_MEMORY / "mesh-log.md",                _OBSIDIAN / "mesh_log.md"),
    # Heartbeat
    (_MEMORY / "heartbeat-state.json",       _OBSIDIAN / "heartbeat-state.json"),
]

_INTERVAL_S = 3600  # sync cada hora


def _inject_timestamp(content: str, source: Path) -> str:
    """Añade timestamp de última sync al final del archivo."""
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    footer = f"\n\n---\n*Sincronizado desde `{source.name}` el {ts}*"
    # Replace existing footer if present
    if "\n---\n*Sincronizado" in content:
        content = content[:content.rfind("\n---\n*Sincronizado")]
    return content + footer


def sync_now() -> dict:
    """Sync síncrono — copia todos los archivos existentes a Obsidian."""
    _OBSIDIAN.mkdir(parents=True, exist_ok=True)
    results = {"ok": 0, "skipped": 0, "errors": 0}

    for src, dst in _SYNC_MAP:
        if not src.exists():
            results["skipped"] += 1
            continue
        try:
            content = src.read_text(encoding="utf-8")
            # Add timestamp footer for markdown files
            if src.suffix == ".md":
                content = _inject_timestamp(content, src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            results["ok"] += 1
        except Exception as e:
            logger.warning("obsidian_sync_file_error", src=str(src), error=str(e))
            results["errors"] += 1

    # Write an index / dashboard note
    _write_dashboard()
    return results


def _write_dashboard() -> None:
    """Genera AURA_Dashboard.md — vista rápida del estado del sistema."""
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # Count pending tasks
    tasks_path = _MEMORY / "shared" / "tasks.md"
    pending_aura, pending_hermes = 0, 0
    if tasks_path.exists():
        text = tasks_path.read_text()
        in_aura = in_hermes = False
        for line in text.splitlines():
            if "## Pendientes AURA" in line:
                in_aura, in_hermes = True, False
            elif "## Pendientes Hermes" in line:
                in_aura, in_hermes = False, True
            elif line.startswith("## "):
                in_aura = in_hermes = False
            elif "- [ ]" in line:
                if in_aura:
                    pending_aura += 1
                elif in_hermes:
                    pending_hermes += 1

    # Mesh log last 3
    mesh_lines = []
    mesh_log = _MEMORY / "mesh-log.md"
    if mesh_log.exists():
        lines = [l for l in mesh_log.read_text().splitlines() if l.strip()]
        mesh_lines = lines[-3:] if len(lines) >= 3 else lines

    content = f"""# AURA Dashboard
*Actualizado: {ts}*

## Estado del Sistema

| Agente | Estado |
|--------|--------|
| ✨ AURA | 🟢 Online |
| ⚡ Hermes | 🟢 Online |
| 📱 Instagram | ✅ @royaluniondesign |
| 🖼 FLUX.1 | ✅ Pollinations.ai (FREE) |

## Tareas Pendientes

- 🟣 **AURA**: {pending_aura} tareas
- ⚡ **Hermes**: {pending_hermes} tareas

→ [[shared_tasks]] para detalle completo

## Últimas Conversaciones Mesh

```
{chr(10).join(mesh_lines) if mesh_lines else 'Sin conversaciones aún'}
```

## Comandos Rápidos (Telegram)

| Comando | Qué hace |
|---------|----------|
| `/social` | Estado publicación social |
| `/social queue` | Cola programada |
| `/post instagram sobre X` | Publicar ahora |
| `/post instagram schedule mañana 18h sobre X` | Programar |
| `/mesh` | Estado AURA + Hermes |
| `/mesh chat <msg>` | Hablar a los dos agentes |
| `/hermes <tarea>` | Delegar a Hermes |
| `/galeria` | Ver borradores de imágenes |

## Links

- [[shared_tasks]] — Tareas pendientes
- [[shared_projects]] — Proyectos activos
- [[AURA_MEMORY]] — Memoria completa de AURA
- [[hermes]] — Info sobre Hermes
- [[mesh_log]] — Log de conversaciones inter-agente
- [[social_roadmap]] — Roadmap social F1-F6
"""
    (_OBSIDIAN / "AURA_Dashboard.md").write_text(content, encoding="utf-8")


async def start_obsidian_sync_loop() -> None:
    """Loop que sincroniza Obsidian cada hora."""
    # Sync inmediato al arrancar
    await asyncio.sleep(5)
    result = sync_now()
    logger.info("obsidian_sync_startup", **result)

    while True:
        await asyncio.sleep(_INTERVAL_S)
        result = sync_now()
        logger.info("obsidian_sync_hourly", **result)
