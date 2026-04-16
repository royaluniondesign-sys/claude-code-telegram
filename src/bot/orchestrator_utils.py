"""Shared utilities and helpers for MessageOrchestrator.

Contains:
- Secret-redaction patterns and helpers
- Tool icon mapping
- Verbose progress formatting
- Stream callback factory
- Image sending helper
- Typing heartbeat
- HTML escaping
- Bash passthrough
- Unknown command handler
- Delegation helpers (Ollama → CLI)
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

from ..claude.sdk_integration import StreamUpdate
from .utils.draft_streamer import DraftStreamer
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Keep old name for internal compat
_redact_secrets = redact_secrets

# ---------------------------------------------------------------------------
# Tool icons
# ---------------------------------------------------------------------------

# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


# Keep old name for internal compat
_tool_icon = tool_icon

# ---------------------------------------------------------------------------
# HTML escaping
# ---------------------------------------------------------------------------


def escape_html(text: str) -> str:
    """Escape HTML special chars for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Verbose progress formatting
# ---------------------------------------------------------------------------


def format_verbose_progress(
    activity_log: List[Dict[str, Any]],
    verbose_level: int,
    start_time: float,
) -> str:
    """Build the progress message text based on activity so far."""
    if not activity_log:
        return "Working..."

    elapsed = time.time() - start_time
    lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

    for entry in activity_log[-15:]:  # Show last 15 entries max
        kind = entry.get("kind", "tool")
        if kind == "text":
            # Claude's intermediate reasoning/commentary
            snippet = entry.get("detail", "")
            if verbose_level >= 2:
                lines.append(f"\U0001f4ac {snippet}")
            else:
                # Level 1: one short line
                lines.append(f"\U0001f4ac {snippet[:80]}")
        else:
            # Tool call
            icon = tool_icon(entry["name"])
            if verbose_level >= 2 and entry.get("detail"):
                lines.append(f"{icon} {entry['name']}: {entry['detail']}")
            else:
                lines.append(f"{icon} {entry['name']}")

    if len(activity_log) > 15:
        lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool input summarizer
# ---------------------------------------------------------------------------


def summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Return a short summary of tool input for verbose level 2."""
    if not tool_input:
        return ""
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path") or tool_input.get("path", "")
        if path:
            # Show just the filename, not the full path
            return path.rsplit("/", 1)[-1]
    if tool_name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern", "")
        if pattern:
            return pattern[:60]
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            return redact_secrets(cmd[:100])[:80]
    if tool_name in ("WebFetch", "WebSearch"):
        return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        if desc:
            return desc[:60]
    # Generic: show first key's value
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""


# ---------------------------------------------------------------------------
# Typing heartbeat
# ---------------------------------------------------------------------------


def start_typing_heartbeat(
    chat: Any,
    interval: float = 2.0,
) -> "asyncio.Task[None]":
    """Start a background typing indicator task.

    Sends typing every *interval* seconds, independently of stream events.
    Cancel the returned task in a ``finally`` block.
    """

    async def _heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await chat.send_action("typing")
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    return asyncio.create_task(_heartbeat())


# ---------------------------------------------------------------------------
# Stream callback factory
# ---------------------------------------------------------------------------


def make_stream_callback(
    verbose_level: int,
    progress_msg: Any,
    tool_log: List[Dict[str, Any]],
    start_time: float,
    mcp_images: Optional[List[ImageAttachment]] = None,
    approved_directory: Optional[Path] = None,
    draft_streamer: Optional[DraftStreamer] = None,
) -> Optional[Callable[[StreamUpdate], Any]]:
    """Create a stream callback for verbose progress updates.

    When *mcp_images* is provided, the callback also intercepts
    ``send_image_to_user`` tool calls and collects validated
    :class:`ImageAttachment` objects for later Telegram delivery.

    When *draft_streamer* is provided, tool activity and assistant
    text are streamed to the user in real time via
    ``sendMessageDraft``.

    Returns None when verbose_level is 0 **and** no MCP image
    collection or draft streaming is requested.
    Typing indicators are handled by a separate heartbeat task.
    """
    need_mcp_intercept = mcp_images is not None and approved_directory is not None

    if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
        return None

    last_edit_time = [0.0]  # mutable container for closure

    async def _on_stream(update_obj: StreamUpdate) -> None:
        # Intercept send_image_to_user MCP tool calls.
        # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
        # so match both the bare name and the namespaced variant.
        if update_obj.tool_calls and need_mcp_intercept:
            for tc in update_obj.tool_calls:
                tc_name = tc.get("name", "")
                if tc_name == "send_image_to_user" or tc_name.endswith(
                    "__send_image_to_user"
                ):
                    tc_input = tc.get("input", {})
                    file_path = tc_input.get("file_path", "")
                    caption = tc_input.get("caption", "")
                    img = validate_image_path(
                        file_path, approved_directory, caption
                    )
                    if img:
                        mcp_images.append(img)

        # Capture tool calls
        if update_obj.tool_calls:
            for tc in update_obj.tool_calls:
                name = tc.get("name", "unknown")
                detail = summarize_tool_input(name, tc.get("input", {}))
                if verbose_level >= 1:
                    tool_log.append(
                        {"kind": "tool", "name": name, "detail": detail}
                    )
                if draft_streamer:
                    icon = tool_icon(name)
                    line = (
                        f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                    )
                    await draft_streamer.append_tool(line)

        # Capture assistant text (reasoning / commentary)
        if update_obj.type == "assistant" and update_obj.content:
            text = update_obj.content.strip()
            if text:
                first_line = text.split("\n", 1)[0].strip()
                if first_line:
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "text", "detail": first_line[:120]}
                        )
                    if draft_streamer:
                        await draft_streamer.append_tool(
                            f"\U0001f4ac {first_line[:120]}"
                        )

        # Stream text to user via draft (prefer token deltas;
        # skip full assistant messages to avoid double-appending)
        if draft_streamer and update_obj.content:
            if update_obj.type == "stream_delta":
                await draft_streamer.append_text(update_obj.content)

        # Throttle progress message edits to avoid Telegram rate limits
        if not draft_streamer and verbose_level >= 1:
            now = time.time()
            if (now - last_edit_time[0]) >= 2.0 and tool_log:
                last_edit_time[0] = now
                new_text = format_verbose_progress(
                    tool_log, verbose_level, start_time
                )
                try:
                    await progress_msg.edit_text(new_text)
                except Exception:
                    pass

    return _on_stream


# ---------------------------------------------------------------------------
# Image sending helper
# ---------------------------------------------------------------------------


async def send_images(
    update: Update,
    images: List[ImageAttachment],
    reply_to_message_id: Optional[int] = None,
    caption: Optional[str] = None,
    caption_parse_mode: Optional[str] = None,
) -> bool:
    """Send extracted images as a media group (album) or documents.

    If *caption* is provided and fits (<=1024 chars), it is attached to the
    photo / first album item so text + images appear as one message.

    Returns True if the caption was successfully embedded in the photo message.
    """
    photos: List[ImageAttachment] = []
    documents: List[ImageAttachment] = []
    for img in images:
        if should_send_as_photo(img.path):
            photos.append(img)
        else:
            documents.append(img)

    # Telegram caption limit
    use_caption = bool(
        caption and len(caption) <= 1024 and photos and not documents
    )
    caption_sent = False

    # Send raster photos as a single album (Telegram groups 2-10 items)
    if photos:
        try:
            if len(photos) == 1:
                with open(photos[0].path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        reply_to_message_id=reply_to_message_id,
                        caption=caption if use_caption else None,
                        parse_mode=caption_parse_mode if use_caption else None,
                    )
                caption_sent = use_caption
            else:
                media = []
                file_handles = []
                for idx, img in enumerate(photos[:10]):
                    fh = open(img.path, "rb")  # noqa: SIM115
                    file_handles.append(fh)
                    media.append(
                        InputMediaPhoto(
                            media=fh,
                            caption=caption if use_caption and idx == 0 else None,
                            parse_mode=(
                                caption_parse_mode
                                if use_caption and idx == 0
                                else None
                            ),
                        )
                    )
                try:
                    await update.message.chat.send_media_group(
                        media=media,
                        reply_to_message_id=reply_to_message_id,
                    )
                    caption_sent = use_caption
                finally:
                    for fh in file_handles:
                        fh.close()
        except Exception as e:
            logger.warning("Failed to send photo album", error=str(e))

    # Send SVGs / large files as documents (one by one — can't mix in album)
    for img in documents:
        try:
            with open(img.path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=img.path.name,
                    reply_to_message_id=reply_to_message_id,
                )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(
                "Failed to send document image",
                path=str(img.path),
                error=str(e),
            )

    return caption_sent


# ---------------------------------------------------------------------------
# Bash passthrough
# ---------------------------------------------------------------------------


async def bash_passthrough(update: Update, command: str) -> bool:
    """Execute shell command directly without Claude. Returns True if handled."""
    try:
        current_dir = str(Path.home())
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=current_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode().strip()
        err = stderr.decode().strip()

        result = output if output else err if err else "(no output)"
        # Truncate for Telegram's 4096 char limit
        if len(result) > 3900:
            result = result[:3900] + "\n... (truncated)"

        await update.message.reply_text(
            f"<pre>{escape_html(result)}</pre>",
            parse_mode="HTML",
        )
        return True
    except asyncio.TimeoutError:
        await update.message.reply_text("⏱ Command timed out (30s)")
        return True
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return True


# ---------------------------------------------------------------------------
# Delegation helpers (Ollama -> CLI)
# ---------------------------------------------------------------------------

_DELEGATE_RE = re.compile(r"<<DELEGATE:(\w+)>>\s*(.*)", re.DOTALL)

_CLI_MAP: Dict[str, Dict[str, Any]] = {
    # shell — fastest, no LLM, deterministic
    "sh":       {"cmd": "bash",     "mode": "sh",       "emoji": "⚡"},
    "bash":     {"cmd": "bash",     "mode": "sh",       "emoji": "⚡"},
    "shell":    {"cmd": "bash",     "mode": "sh",       "emoji": "⚡"},
    # cline — local Ollama, zero cost, code editing
    "cline":    {"cmd": "cline",    "mode": "cline",    "emoji": "🟣"},
    # opencode — free tier via OpenRouter, code gen/analysis
    "opencode": {"cmd": "opencode", "mode": "opencode", "emoji": "🔶"},
    # codex — OpenAI subscription, fast single-file code gen
    "codex":    {"cmd": "codex",    "mode": "codex",    "emoji": "🟢"},
    # claude — Anthropic subscription (escalation only)
    "claude":   {"cmd": "claude",   "mode": "claude",   "emoji": "🟠"},
}


def parse_delegation(content: str) -> Optional[tuple]:  # type: ignore[type-arg]
    """Parse <<DELEGATE:cli_name>> from Ollama response."""
    m = _DELEGATE_RE.search(content)
    if not m:
        return None
    cli_name = m.group(1).lower().strip()
    cli_prompt = m.group(2).strip()
    if cli_name not in _CLI_MAP or not cli_prompt:
        return None
    return cli_name, cli_prompt


async def execute_cli(
    cli_name: str, prompt: str, cwd: str, timeout: int = 120
) -> str:
    """Execute a CLI tool and return its output."""
    import os
    import shutil

    info = _CLI_MAP.get(cli_name)
    if not info:
        return f"Unknown CLI: {cli_name}"

    extra_paths = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin")
    env_path = f"{extra_paths}:{os.environ.get('PATH', '')}"
    cmd_path = shutil.which(info["cmd"], path=env_path)
    if not cmd_path:
        return f"{cli_name} not installed."

    env = os.environ.copy()
    env["PATH"] = env_path

    # Build command per CLI type (all non-interactive)
    mode = info["mode"]
    if mode == "sh":
        # bash -c "command"
        args = [cmd_path, "-c", prompt]
    elif mode == "cline":
        # cline -m qwen2.5:7b -a "prompt" -y  (act + yolo = non-interactive)
        args = [cmd_path, "-m", "qwen2.5:7b", "-a", prompt, "-y"]
    elif mode == "opencode":
        # opencode run "prompt"
        args = [cmd_path, "run", prompt]
    elif mode == "codex":
        # codex exec "prompt" --full-auto
        args = [cmd_path, "exec", prompt, "--full-auto"]
    elif mode == "claude":
        # claude -p "prompt" --model sonnet
        args = [cmd_path, "-p", prompt, "--model", "sonnet", "--output-format", "text"]
    else:
        args = [cmd_path, prompt]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode().strip()
        if not output and stderr:
            output = stderr.decode().strip()
        # Parse opencode JSON output to extract text
        if cli_name == "opencode" and output:
            output = parse_opencode_json(output)
        return output or "(no output)"
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"{cli_name} timed out after {timeout}s"
    except Exception as e:
        return f"{cli_name} error: {e}"


def parse_opencode_json(raw: str) -> str:
    """Extract text parts from opencode --format json output."""
    import json as _json
    texts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
            if event.get("type") == "text":
                part = event.get("part", {})
                text = part.get("text", "")
                if text:
                    texts.append(text)
        except _json.JSONDecodeError:
            continue
    return "\n".join(texts) if texts else raw
