"""File system tools — read, write, list files."""
from __future__ import annotations
from pathlib import Path
from src.actions.registry import aura_tool

_HOME = Path.home()


def _safe_path(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = _HOME / p
    # Prevent path traversal outside home
    p.resolve().relative_to(_HOME)
    return p


@aura_tool(
    name="file_read",
    description="Read the contents of a file on Ricardo's Mac.",
    category="files",
    parameters={"path": {"type": "str", "description": "Absolute or ~/relative file path"}},
)
async def file_read(path: str) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"File not found: {p}"
    content = p.read_text(encoding="utf-8", errors="replace")
    return content[:4000] if len(content) > 4000 else content


@aura_tool(
    name="file_write",
    description="Write content to a file on Ricardo's Mac.",
    category="files",
    parameters={
        "path":    {"type": "str", "description": "File path"},
        "content": {"type": "str", "description": "Content to write"},
    },
)
async def file_write(path: str, content: str) -> str:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"✅ Written: {p} ({len(content)} chars)"


@aura_tool(
    name="file_list",
    description="List files in a directory.",
    category="files",
    parameters={
        "path":    {"type": "str", "description": "Directory path"},
        "pattern": {"type": "str", "description": "Glob pattern (e.g. *.py)"},
    },
)
async def file_list(path: str = "~", pattern: str = "*") -> str:
    p = _safe_path(path)
    if not p.is_dir():
        return f"Not a directory: {p}"
    entries = sorted(p.glob(pattern))[:50]
    lines = [f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries]
    return "\n".join(lines) or "(empty)"
