"""Hermes bridge tool — delegate tasks to Hermes (OpenClaw) from AURA."""
from __future__ import annotations

import asyncio
import json
from src.actions.registry import aura_tool


def _extract_text(data: object) -> str | None:
    if isinstance(data, dict):
        for key in ("text", "reply", "content", "message"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        payloads = data.get("result", {}).get("payloads", [])  # type: ignore[union-attr]
        texts = [p.get("text", "") for p in payloads if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n".join(texts).strip()
        if "result" in data:  # type: ignore[operator]
            return _extract_text(data["result"])  # type: ignore[index]
    if isinstance(data, list):
        parts = [_extract_text(item) for item in data]
        parts = [p for p in parts if p]
        return "\n".join(parts) if parts else None
    return None


@aura_tool(
    name="hermes_ask",
    description="Ask Hermes (AURA's sibling AI agent @rudserverbot, GPT-oss-120b) a question or delegate a task. Use for coordinating agents or tasks better suited for Hermes.",
    category="system",
    parameters={
        "message": {"type": "str", "description": "Message or task to send to Hermes"},
    },
)
async def hermes_ask(message: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/opt/homebrew/bin/openclaw", "agent", "--json", message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        raw = stdout.decode(errors="replace").strip()

        # Parse JSON — openclaw returns multiple JSON lines, take the last valid one
        lines = [l for l in raw.splitlines() if l.strip().startswith("{")]
        for line in reversed(lines):
            try:
                data = json.loads(line)
                extracted = _extract_text(data)
                if extracted:
                    return f"[Hermes]: {extracted}"
            except Exception:
                continue

        return f"[Hermes raw]: {raw[:800]}" if raw else "Hermes returned no response"

    except asyncio.TimeoutError:
        return "❌ Hermes timeout (60s)"
    except FileNotFoundError:
        return "❌ openclaw not found at /opt/homebrew/bin/openclaw"
    except Exception as e:
        return f"❌ hermes_ask error: {e}"


@aura_tool(
    name="hermes_task",
    description="Delegate a complex multi-step task to Hermes with full agent planning. Hermes will plan, execute, and report back.",
    category="system",
    parameters={
        "task": {"type": "str", "description": "Detailed task description for Hermes to execute"},
        "context": {"type": "str", "description": "Additional context or constraints (optional)", "optional": True},
    },
)
async def hermes_task(task: str, context: str | None = None) -> str:
    full_message = task
    if context:
        full_message = f"{task}\n\nContexto adicional: {context}"
    return await hermes_ask(full_message)
