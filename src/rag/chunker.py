"""Smart text chunker for AURA RAG."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List


def _chunk_id(source: str, content: str) -> str:
    """Generate a stable 16-char ID from source + content."""
    return hashlib.sha256((source + content).encode()).hexdigest()[:16]


def _make_chunk(source: str, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _chunk_id(source, content),
        "content": content.strip(),
        "source": source,
        "metadata": metadata,
    }


def chunk_markdown(text: str, source: str) -> List[Dict[str, Any]]:
    """Split markdown at ## headers, max 500 chars per chunk, include header as context prefix."""
    chunks: List[Dict[str, Any]] = []
    # Split on ## or # headers
    sections = re.split(r"(?m)^(#{1,3} .+)$", text)

    current_header = ""
    current_body = ""

    def flush(header: str, body: str) -> None:
        body = body.strip()
        if not body:
            return
        prefix = f"{header}\n" if header else ""
        # Sub-split if too long
        combined = prefix + body
        if len(combined) <= 500:
            chunks.append(_make_chunk(source, combined, {"header": header, "type": "markdown"}))
        else:
            # Split at paragraph boundaries
            paragraphs = re.split(r"\n{2,}", body)
            window = prefix
            for para in paragraphs:
                candidate = window + para + "\n\n"
                if len(candidate) > 500 and window.strip():
                    chunks.append(_make_chunk(source, window.strip(), {"header": header, "type": "markdown"}))
                    window = prefix + para + "\n\n"
                else:
                    window = candidate
            if window.strip():
                chunks.append(_make_chunk(source, window.strip(), {"header": header, "type": "markdown"}))

    for part in sections:
        if re.match(r"^#{1,3} ", part):
            flush(current_header, current_body)
            current_header = part.strip()
            current_body = ""
        else:
            current_body += part

    flush(current_header, current_body)
    return [c for c in chunks if c["content"]]


def chunk_text(text: str, source: str) -> List[Dict[str, Any]]:
    """Split at paragraph boundaries, max 400 chars, 50-char overlap."""
    chunks: List[Dict[str, Any]] = []
    paragraphs = re.split(r"\n{2,}", text.strip())

    window = ""
    prev_tail = ""  # overlap text from previous chunk

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        candidate = (prev_tail + "\n" + para).strip() if prev_tail else para
        if len(window) + len(para) + 1 > 400 and window:
            chunks.append(_make_chunk(source, window.strip(), {"type": "text"}))
            # Keep last 50 chars as overlap
            prev_tail = window[-50:] if len(window) > 50 else window
            window = para
        else:
            window = (window + "\n\n" + para).strip() if window else para

    if window.strip():
        chunks.append(_make_chunk(source, window.strip(), {"type": "text"}))

    return [c for c in chunks if c["content"]]


_LOG_CHUNK_MAX_CHARS = 1500  # safe ceiling for nomic-embed-text context limit
_LOG_LINE_MAX_CHARS = 300    # truncate individual log lines (JSON blobs can be huge)


def _truncate_line(line: str, max_chars: int = _LOG_LINE_MAX_CHARS) -> str:
    return line[:max_chars] + "…" if len(line) > max_chars else line


def chunk_logs(text: str, source: str) -> List[Dict[str, Any]]:
    """Split log files at timestamp patterns, '---' separators, or 'Message Group' blocks.

    Caps each chunk at _LOG_CHUNK_MAX_CHARS and truncates individual long lines to
    avoid exceeding Ollama's embedding context window.
    """
    chunks: List[Dict[str, Any]] = []
    ts_pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}"
    )
    msg_group_pattern = re.compile(r"^Message Group: ")
    diag_timestamp_pattern = re.compile(r"^\{\"timestamp\":\"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    lines = text.splitlines()
    window_lines: List[str] = []
    window_chars: int = 0

    def flush_window() -> None:
        nonlocal window_chars
        if not window_lines:
            return
        content = "\n".join(window_lines)
        if content.strip():
            chunks.append(_make_chunk(source, content, {"type": "log"}))
        window_lines.clear()
        window_chars = 0

    for line in lines:
        line = _truncate_line(line)
        stripped = line.strip()

        is_msg_group = msg_group_pattern.match(line)
        is_diag_ts = diag_timestamp_pattern.match(line)
        is_separator = stripped == "---" or ts_pattern.match(stripped) or is_msg_group or is_diag_ts

        if is_separator and window_lines:
            if is_msg_group or is_diag_ts:
                flush_window()
            elif len(window_lines) >= 20:
                flush_window()

        # Flush if adding this line would exceed char limit
        if window_chars + len(line) + 1 > _LOG_CHUNK_MAX_CHARS and window_lines:
            flush_window()

        window_lines.append(line)
        window_chars += len(line) + 1

        # Hard cap on line count
        if len(window_lines) >= 50:
            flush_window()

    flush_window()
    return [c for c in chunks if c["content"]]


def chunk_code(text: str, source: str) -> List[Dict[str, Any]]:
    """Split Python code into logical chunks based on classes and functions."""
    chunks: List[Dict[str, Any]] = []
    
    # Split by class or top-level function
    sections = re.split(r"(?m)^(class |def )", text)
    
    current_section = ""
    prefix = "" # Imports, module-level vars at the top
    
    # The first element is everything before the first class/def
    if sections:
        prefix = sections[0].strip()
        sections = sections[1:]
    
    # sections is now [tag, body, tag, body, ...] where tag is "class " or "def "
    for i in range(0, len(sections), 2):
        tag = sections[i]
        body = sections[i+1] if i+1 < len(sections) else ""
        content = tag + body
        
        # If it's a large class/function, sub-chunk it
        if len(content) > 1500:
            lines = content.splitlines()
            current = ""
            for line in lines:
                if len(current) + len(line) + 1 > 1500:
                    chunks.append(_make_chunk(source, current.strip(), {"type": "code"}))
                    current = line + "\n"
                else:
                    current += line + "\n"
            if current.strip():
                chunks.append(_make_chunk(source, current.strip(), {"type": "code"}))
        else:
            chunks.append(_make_chunk(source, content.strip(), {"type": "code"}))
            
    if not chunks and prefix:
        # If no classes/defs found, just chunk the whole thing as text
        return chunk_text(text, source)
        
    return [c for c in chunks if c["content"]]
