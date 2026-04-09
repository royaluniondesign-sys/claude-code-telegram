"""Task Journal — persistent per-task markdown logs for the AURA auto-executor.

Each task gets its own markdown file in ~/.aura/task_journal/{task_id}.md.
Records attempts, learnings, and summaries so future runs can learn from
past execution history.

Journal directory: ~/.aura/task_journal/
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
import structlog

logger = structlog.get_logger()

_JOURNAL_DIR = Path.home() / ".aura" / "task_journal"

# Status constants
_STATUS_IN_PROGRESS = "in_progress"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"

# Section markers used when appending to existing files
_SECTION_ATTEMPTS = "## Attempts"
_SECTION_LEARNINGS = "## Learnings"
_SECTION_SUMMARY = "## Summary"


def _journal_path(task_id: str) -> Path:
    """Return the absolute path for a task journal file."""
    return _JOURNAL_DIR / f"{task_id}.md"


def _ensure_journal_dir() -> None:
    """Create the journal directory if it does not exist."""
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _build_initial_content(
    task_id: str,
    title: str,
    brain: str,
    started: str,
    context: str,
) -> str:
    """Build the initial markdown content for a new task journal.

    Returns a new string — nothing is mutated.
    """
    context_body = context.strip() if context else "_No context provided._"
    return (
        f"# Task: {title}\n"
        f"**ID:** {task_id}\n"
        f"**Brain:** {brain}\n"
        f"**Started:** {started}\n"
        f"**Status:** {_STATUS_IN_PROGRESS}\n"
        f"\n"
        f"## Context\n"
        f"{context_body}\n"
        f"\n"
        f"## Attempts\n"
        f"\n"
        f"## Learnings\n"
        f"\n"
        f"## Summary\n"
    )


def start_task(
    task_id: str,
    title: str,
    brain: str,
    context: str = "",
) -> Path:
    """Create a new journal file for a task.

    If the journal already exists it is left untouched and its path is
    returned — this makes the function idempotent so callers can safely
    call it on retry without clobbering earlier attempt data.

    Args:
        task_id: Unique identifier for the task (matches tasks.json id).
        title:   Human-readable task title.
        brain:   Name of the brain/model handling this task.
        context: Optional extra context to record at the top of the journal.

    Returns:
        Path to the journal file.
    """
    _ensure_journal_dir()
    path = _journal_path(task_id)

    if path.exists():
        logger.debug("task_journal.exists", task_id=task_id, path=str(path))
        return path

    started = _now_iso()
    content = _build_initial_content(task_id, title, brain, started, context)
    path.write_text(content, encoding="utf-8")

    logger.info("task_journal.created", task_id=task_id, title=title, brain=brain)
    return path


def log_attempt(
    task_id: str,
    attempt: int,
    command: str,
    output: str,
    success: bool,
) -> None:
    """Append an attempt block to the task journal.

    Args:
        task_id:  Target task id.
        attempt:  Attempt number (1-based).
        command:  The command or action that was executed.
        output:   Stdout/stderr or result text from the attempt.
        success:  Whether the attempt succeeded.
    """
    path = _journal_path(task_id)
    if not path.exists():
        logger.warning("task_journal.missing_on_log_attempt", task_id=task_id)
        return

    status_icon = "✅ Success" if success else "❌ Failed"
    # Fence the output block; escape any closing fences inside the output
    safe_output = output.replace("```", "~~~")
    block = (
        f"\n### Attempt {attempt} — {status_icon}\n"
        f"**Command:** `{command}`\n"
        f"**Output:**\n"
        f"```\n"
        f"{safe_output}\n"
        f"```\n"
    )

    original = path.read_text(encoding="utf-8")
    # Insert the new attempt block just before ## Learnings
    updated = _insert_before_section(original, _SECTION_LEARNINGS, block)
    path.write_text(updated, encoding="utf-8")

    logger.debug(
        "task_journal.attempt_logged",
        task_id=task_id,
        attempt=attempt,
        success=success,
    )


def log_learning(task_id: str, learning: str) -> None:
    """Append a learning bullet to the Learnings section.

    Args:
        task_id:  Target task id.
        learning: A concise sentence describing what was learned.
    """
    path = _journal_path(task_id)
    if not path.exists():
        logger.warning("task_journal.missing_on_log_learning", task_id=task_id)
        return

    bullet = f"- {learning.strip()}\n"
    original = path.read_text(encoding="utf-8")
    updated = _insert_before_section(original, _SECTION_SUMMARY, bullet)
    path.write_text(updated, encoding="utf-8")

    logger.debug("task_journal.learning_logged", task_id=task_id)


def complete_task_journal(task_id: str, summary: str) -> None:
    """Mark the task journal as completed and append a summary.

    Sets **Status:** to ``completed`` and writes *summary* under the
    Summary section.  Call ``_mark_status(task_id, _STATUS_FAILED)``
    separately if you need to record a failure instead.

    Args:
        task_id: Target task id.
        summary: Human-readable summary of what was accomplished.
    """
    path = _journal_path(task_id)
    if not path.exists():
        logger.warning("task_journal.missing_on_complete", task_id=task_id)
        return

    original = path.read_text(encoding="utf-8")

    # Update status line
    with_status = re.sub(
        r"(\*\*Status:\*\*\s*)\S+",
        rf"\g<1>{_STATUS_COMPLETED}",
        original,
        count=1,
    )

    # Append summary text after the ## Summary heading
    summary_body = f"\n{summary.strip()}\n"
    updated = _insert_after_section(with_status, _SECTION_SUMMARY, summary_body)
    path.write_text(updated, encoding="utf-8")

    logger.info("task_journal.completed", task_id=task_id)


def search_similar(query: str, max_results: int = 5) -> list[str]:
    """Search past journals for lines matching the query.

    Uses ``grep`` (via subprocess) for fast filesystem search.  Returns
    a de-duplicated list of matching lines / snippets from the Learnings
    sections, capped at *max_results*.

    Args:
        query:       Plain-text or regex pattern to search for.
        max_results: Maximum number of result snippets to return.

    Returns:
        List of matching learning snippets (may be empty).
    """
    if not _JOURNAL_DIR.exists():
        return []

    try:
        result = subprocess.run(  # noqa: S603
            [
                "grep",
                "-r",          # recursive
                "-i",          # case-insensitive
                "-h",          # suppress filenames
                "--include=*.md",
                "-A", "0",
                query,
                str(_JOURNAL_DIR),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("task_journal.search_failed", error=str(exc))
        return []

    if result.returncode not in (0, 1):
        # returncode 1 means no matches — anything else is an actual error
        logger.warning(
            "task_journal.grep_error",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )
        return []

    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]

    # Prefer lines that look like learning bullets
    learning_lines = [ln for ln in lines if ln.startswith("- ")]
    other_lines = [ln for ln in lines if not ln.startswith("- ")]

    ranked = learning_lines + other_lines
    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for ln in ranked:
        if ln not in seen:
            seen.add(ln)
            unique.append(ln)
        if len(unique) >= max_results:
            break

    logger.debug(
        "task_journal.search_results",
        query=query,
        count=len(unique),
    )
    return unique


def get_learnings_summary(max_items: int = 10) -> str:
    """Return the most recent learnings across all task journals.

    Journals are scanned newest-first (by mtime). Returns a
    newline-separated string of bullet items ready for prompt injection,
    or an empty string when no learnings exist.

    Args:
        max_items: Maximum number of learning bullets to include.
    """
    if not _JOURNAL_DIR.exists():
        return ""

    # Sort by mtime descending so we read newest files first
    journal_files = sorted(
        _JOURNAL_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    collected: list[str] = []

    for journal_file in journal_files:
        if len(collected) >= max_items:
            break
        try:
            text = journal_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "task_journal.read_error",
                file=str(journal_file),
                error=str(exc),
            )
            continue

        bullets = _extract_learnings(text)
        for bullet in bullets:
            if len(collected) >= max_items:
                break
            collected.append(bullet)

    if not collected:
        return ""

    return "\n".join(collected)


# ---------------------------------------------------------------------------
# Private helpers — pure string operations, no I/O
# ---------------------------------------------------------------------------


def _insert_before_section(text: str, section_header: str, block: str) -> str:
    """Return a new string with *block* inserted immediately before *section_header*.

    If the section header is not found the block is appended at the end.
    """
    idx = text.find(f"\n{section_header}")
    if idx == -1:
        return text + block
    return text[:idx] + block + text[idx:]


def _insert_after_section(text: str, section_header: str, block: str) -> str:
    """Return a new string with *block* inserted on the line after *section_header*.

    If the section header is not found the block is appended at the end.
    """
    idx = text.find(f"\n{section_header}")
    if idx == -1:
        return text + block
    header_end = idx + len(f"\n{section_header}")
    return text[:header_end] + block + text[header_end:]


def _extract_learnings(text: str) -> list[str]:
    """Extract bullet items from the Learnings section of a journal.

    Returns a list of bullet strings (with leading ``- ``).
    """
    # Find the Learnings section and stop at the next ## heading
    match = re.search(
        r"## Learnings\n(.*?)(?=\n## |\Z)",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return []

    section_text = match.group(1)
    bullets: list[str] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and len(stripped) > 2:
            bullets.append(stripped)
    return bullets


def _mark_status(task_id: str, status: str) -> None:
    """Update the **Status:** line in a journal (internal helper).

    Args:
        task_id: Target task id.
        status:  One of the _STATUS_* constants.
    """
    path = _journal_path(task_id)
    if not path.exists():
        return
    original = path.read_text(encoding="utf-8")
    updated = re.sub(
        r"(\*\*Status:\*\*\s*)\S+",
        rf"\g<1>{status}",
        original,
        count=1,
    )
    if updated != original:
        path.write_text(updated, encoding="utf-8")
