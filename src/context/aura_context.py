"""AURA Context Engine — builds rich unified system context for all brain calls.

Reads identity + dynamic memory from ~/.aura/brain/ and composes a system
prompt that makes every brain aware of who AURA is, who Ricardo is, and what
AURA has learned so far.

Memory is updated via update_memory() after each learning extraction.
"""

from __future__ import annotations

import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Service ports ────────────────────────────────────────────────────────────
_OPENDESIGN_PORT = 59826
_COMFYUI_PORT = 8188
_TERMORA_PORT = 4030


def _port_open(port: int) -> bool:
    """Check if a local TCP port is listening (fast, no HTTP)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _read_design_md() -> str:
    """Return the royaluniondesign DESIGN.md summary (first 40 lines)."""
    path = Path.home() / "Projects/design-systems/royaluniondesign/DESIGN.md"
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:40]
        return "\n".join(lines)
    except Exception:
        return ""


def build_tool_manifest() -> str:
    """Return a markdown section describing available tools and when to use them.

    Service availability is checked at call time so the manifest reflects
    what's actually running on Ricardo's Mac right now.
    """
    sections: list[str] = ["## Herramientas disponibles en este Mac\n"]

    # ── Design & Content ─────────────────────────────────────────────────────
    if _port_open(_OPENDESIGN_PORT):
        sections.append(
            "### open-design (ACTIVO en puerto 59826)\n"
            "Genera carousels, posts, prototipos HTML/CSS con branding RUD.\n"
            f"- API: `curl -s http://127.0.0.1:{_OPENDESIGN_PORT}/api/generate -d '{{\"brief\":\"...\",\"format\":\"carousel_9_16\"}}'`\n"
            "- Formatos: carousel_9_16, post_1_1, post_4_5, story_9_16, banner_16_9\n"
            "- DESIGN.md: ~/Projects/design-systems/royaluniondesign/DESIGN.md\n"
            "  → Paleta: #0d0d0d negro + #c9a84c gold + #f5f0e8 cream\n"
            "  → Tipografía: Montserrat Black + Playfair Display + Inter\n"
            "**Usar cuando:** Ricardo pida carousel, post, diseño, contenido visual para Instagram/LinkedIn."
        )
    else:
        sections.append(
            "### open-design (OFFLINE)\n"
            "Para activar: `cd ~/Projects/open-design && corepack pnpm tools-dev run web`\n"
            "**Si Ricardo pide diseño:** avísale que open-design está offline y ofrece activarlo."
        )

    # ── Image AI ─────────────────────────────────────────────────────────────
    if _port_open(_COMFYUI_PORT):
        sections.append(
            "### ComfyUI — FLUX.1-dev (ACTIVO en puerto 8188)\n"
            "Genera imágenes fotorrealistas profesionales en ~90s.\n"
            "- Tool MCP: `comfyui_generate(prompt_en_ingles, preset='square')`\n"
            "- Presets: square (1080×1080), portrait (1080×1350), story (1080×1920)\n"
            "- Output: ~/Projects/ComfyUI/output/ComfyUI_*.png\n"
            "**Usar cuando:** Ricardo pida imagen, foto, ilustración, o para acompañar un post."
        )
    else:
        sections.append(
            "### Imagen IA (ComfyUI offline → Pollinations.ai fallback)\n"
            "Pollinations.ai FLUX.1 gratis: `curl 'https://image.pollinations.ai/prompt/ENGLISH_PROMPT?width=1080&height=1080'`\n"
            "**Usar cuando:** Ricardo pida imagen. ComfyUI da más calidad si está activo."
        )

    # ── Social Media ─────────────────────────────────────────────────────────
    sections.append(
        "### Social Media (Instagram + Facebook)\n"
        "- Instagram @royaluniondesign: Meta Graph API, tokens en .env (META_ACCESS_TOKEN)\n"
        "- Tool MCP: `instagram_publish(caption='texto + hashtags', image_path='ruta.png')`\n"
        "- O directo: `instagram_publish(caption='...', prompt='english flux prompt')` (genera imagen sola)\n"
        "**Usar cuando:** Ricardo diga 'publica', 'sube a Instagram', 'programa post'."
    )

    # ── Terminal interactivo ──────────────────────────────────────────────────
    if _port_open(_TERMORA_PORT):
        sections.append(
            "### Termora — Terminal web (ACTIVO en puerto 4030)\n"
            "Provee terminal interactivo accesible desde el móvil.\n"
            "- Info + URL: `curl -s http://localhost:4030/api/info` → devuelve authUrl con token\n"
            "**Usar cuando:** Ricardo necesite shell interactivo, vim, tmux, htop, o SSH desde el móvil."
        )

    # ── Blog ─────────────────────────────────────────────────────────────────
    sections.append(
        "### Blog RUD (rud-web.vercel.app)\n"
        "Publica via GitHub API → commit MDX → Vercel auto-deploys.\n"
        "- Repo: royaluniondesign-sys/rud-web-dev, ruta: src/content/blog/\n"
        "**Usar cuando:** Ricardo diga 'publica en el blog', 'escribe un artículo'."
    )

    # ── Routing hints ─────────────────────────────────────────────────────────
    sections.append(
        "## Reglas de routing inteligente\n"
        "- 'carousel' / 'post de Instagram' / 'diseño' → open-design primero\n"
        "- 'imagen' / 'foto' / 'genera imagen' → ComfyUI (o Pollinations si offline)\n"
        "- 'publica' / 'sube' / 'Instagram' → instagram_publish\n"
        "- 'artículo' / 'blog' / 'post del blog' → GitHub API + MDX\n"
        "- 'terminal' / 'shell desde el móvil' → Termora URL\n"
        "- NO preguntes si debe proceder. Ejecuta y reporta el resultado."
    )

    return "\n\n".join(sections)

_BRAIN_DIR = Path.home() / ".aura" / "brain"
_IDENTITY_FILE = _BRAIN_DIR / "identity.md"
_MEMORY_FILE = _BRAIN_DIR / "memory.md"

# Sections in memory.md that auto-learning writes to
_SECTION_CLIENTS = "## Clientes de RUD"
_SECTION_TASKS = "## Tareas recientes"
_SECTION_NOTES = "## Notas"


def _read_file(path: Path) -> str:
    """Read a file safely, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def build_system_prompt(
    include_memory: bool = True,
    extra_section: str = "",
) -> str:
    """Build a rich system prompt: identity + memory + timestamp.

    Called fresh on every brain request so memory updates are reflected
    immediately without restarting the bot.

    Args:
        include_memory: Whether to include dynamic memory facts.
        extra_section: Optional extra section appended at the end
                       (e.g., executor CLI instructions for Claude brain).
    """
    parts: list[str] = []

    identity = _read_file(_IDENTITY_FILE)
    if identity:
        parts.append(identity)

    if include_memory:
        memory = _read_file(_MEMORY_FILE)
        if memory:
            parts.append(f"---\n{memory}")

    # Tool manifest — what's available RIGHT NOW on Ricardo's Mac
    manifest = build_tool_manifest()
    if manifest:
        parts.append(f"---\n{manifest}")

    # Always include current date/time so the brain knows when "now" is
    now = datetime.now()
    parts.append(
        f"---\nFecha actual: {now.strftime('%Y-%m-%d %H:%M')} (hora local de Ricardo)"
    )

    if extra_section:
        parts.append(f"---\n{extra_section}")

    return "\n\n".join(parts)


def get_memory() -> str:
    """Return the current memory file content."""
    return _read_file(_MEMORY_FILE)


def get_identity() -> str:
    """Return the identity file content."""
    return _read_file(_IDENTITY_FILE)


def update_memory(fact: str, section: str = _SECTION_NOTES) -> None:
    """Append a timestamped fact to a section in memory.md.

    If the section doesn't exist, appends a new one at the bottom.
    """
    _BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now().strftime("%Y-%m-%d")
    entry = f"- {fact} [{date_tag}]"

    content = _read_file(_MEMORY_FILE)
    if not content:
        # Create fresh memory file with section
        _MEMORY_FILE.write_text(
            f"# AURA Memory — Hechos aprendidos\n\n{section}\n{entry}\n",
            encoding="utf-8",
        )
        return

    if section in content:
        # Insert after the section header
        lines = content.splitlines()
        result: list[str] = []
        inserted = False
        i = 0
        while i < len(lines):
            result.append(lines[i])
            if not inserted and lines[i].strip() == section.strip():
                # Find end of this section (next ## or EOF)
                j = i + 1
                while j < len(lines):
                    if lines[j].startswith("## "):
                        break
                    result.append(lines[j])
                    j += 1
                result.append(entry)
                inserted = True
                i = j
                continue
            i += 1
        if not inserted:
            result.append(entry)
        _MEMORY_FILE.write_text("\n".join(result) + "\n", encoding="utf-8")
    else:
        # Append new section
        _MEMORY_FILE.write_text(
            content + f"\n\n{section}\n{entry}\n", encoding="utf-8"
        )


def add_client(email: str, name: str = "", company: str = "", notes: str = "") -> None:
    """Add or update a client in memory."""
    parts = [email]
    if name:
        parts.append(name)
    if company:
        parts.append(f"({company})")
    if notes:
        parts.append(f"— {notes}")
    update_memory(" · ".join(parts), _SECTION_CLIENTS)


def add_task(description: str) -> None:
    """Record a completed task in memory."""
    update_memory(description, _SECTION_TASKS)


def format_for_display() -> str:
    """Format memory for Telegram display."""
    identity = get_identity()
    memory = get_memory()

    lines = ["<b>🧠 AURA — Cerebro</b>\n"]

    # Show memory sections
    if memory:
        for line in memory.splitlines():
            if line.startswith("# "):
                continue  # skip title
            elif line.startswith("## "):
                lines.append(f"\n<b>{line[3:]}</b>")
            elif line.startswith("- "):
                lines.append(f"  {line}")
            elif line.strip():
                lines.append(line)

    if not memory:
        lines.append("Memoria vacía — AURA aún no ha aprendido nada.")

    lines.append(
        "\n💡 <code>/memory add &lt;hecho&gt;</code> · "
        "<code>/memory client &lt;email&gt; &lt;nombre&gt;</code>"
    )
    return "\n".join(lines)


class AuraContext:
    """Singleton-style context manager (stateless reads, stateful writes)."""

    @staticmethod
    def system_prompt(extra: str = "") -> str:
        return build_system_prompt(extra_section=extra)

    @staticmethod
    def learn(fact: str, section: str = _SECTION_NOTES) -> None:
        update_memory(fact, section)

    @staticmethod
    def learn_client(email: str, name: str = "", company: str = "") -> None:
        add_client(email, name, company)

    @staticmethod
    def learn_task(description: str) -> None:
        add_task(description)
