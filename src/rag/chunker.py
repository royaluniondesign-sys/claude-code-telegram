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


def chunk_logs(text: str, source: str) -> List[Dict[str, Any]]:
    """Split log files at timestamp patterns or '---' separators, 20-line windows."""
    chunks: List[Dict[str, Any]] = []
    # Detect timestamp lines: ISO datetime or common log formats
    ts_pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}"
    )

    lines = text.splitlines()
    window_lines: List[str] = []

    def flush_window() -> None:
        if not window_lines:
            return
        content = "\n".join(window_lines)
        if content.strip():
            chunks.append(_make_chunk(source, content, {"type": "log"}))
        window_lines.clear()

    for line in lines:
        is_separator = line.strip() == "---" or ts_pattern.match(line.strip())
        if is_separator and len(window_lines) >= 20:
            flush_window()
        window_lines.append(line)
        if len(window_lines) >= 20 and (is_separator or len(window_lines) >= 25):
            flush_window()

    flush_window()
    return [c for c in chunks if c["content"]]
