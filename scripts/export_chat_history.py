#!/usr/bin/env python3
"""Export Telegram bot chat history from the local SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Iterable


DEFAULT_DB_PATH = Path("/Users/oxyzen/claude-code-telegram/data/bot.db")
DEFAULT_OUTPUT_DIR = Path("/Users/oxyzen/claude-code-telegram/exports")
CHROME_CANDIDATES = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
]


@dataclass
class ChatRow:
    message_id: int
    session_id: str
    user_id: int
    username: str | None
    project_path: str
    session_created_at: str
    session_last_used: str
    timestamp: str
    prompt: str
    response: str
    cost: float
    duration_ms: int | None
    error: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export all bot chat history for a Telegram user from bot.db"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--user-id", type=int, help="Telegram user ID to export")
    parser.add_argument(
        "--session-id",
        action="append",
        dest="session_ids",
        help="Export only the given session ID. Can be repeated.",
    )
    parser.add_argument(
        "--title",
        default="Telegram Bot Chat Export",
        help="Document title used in the exported files",
    )
    parser.add_argument(
        "--bot-handle",
        default="@rudagency_bot",
        help="Bot handle shown in the report",
    )
    parser.add_argument(
        "--skip-pdf",
        action="store_true",
        help="Do not attempt to print the HTML export to PDF",
    )
    return parser.parse_args()


def pick_default_user(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT user_id
        FROM messages
        WHERE user_id != 0
        GROUP BY user_id
        ORDER BY COUNT(*) DESC, MAX(timestamp) DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("No user messages found in the database.")
    return int(row[0])


def fetch_rows(
    conn: sqlite3.Connection, user_id: int, session_ids: list[str] | None
) -> list[ChatRow]:
    query = """
        SELECT
            m.message_id,
            m.session_id,
            m.user_id,
            u.telegram_username,
            s.project_path,
            s.created_at,
            s.last_used,
            m.timestamp,
            m.prompt,
            COALESCE(m.response, ''),
            COALESCE(m.cost, 0.0),
            m.duration_ms,
            m.error
        FROM messages m
        JOIN sessions s ON s.session_id = m.session_id
        LEFT JOIN users u ON u.user_id = m.user_id
        WHERE m.user_id = ?
    """
    params: list[object] = [user_id]

    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        query += f" AND m.session_id IN ({placeholders})"
        params.extend(session_ids)

    query += " ORDER BY m.timestamp ASC, m.message_id ASC"
    rows = conn.execute(query, params).fetchall()

    return [
        ChatRow(
            message_id=row[0],
            session_id=row[1],
            user_id=row[2],
            username=row[3],
            project_path=row[4],
            session_created_at=row[5],
            session_last_used=row[6],
            timestamp=row[7],
            prompt=row[8],
            response=row[9],
            cost=float(row[10] or 0.0),
            duration_ms=row[11],
            error=row[12],
        )
        for row in rows
    ]


def iso_to_local(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def build_summary(rows: list[ChatRow]) -> dict[str, object]:
    if not rows:
        raise SystemExit("No chat rows matched the requested filters.")

    sessions = []
    by_session: dict[str, list[ChatRow]] = defaultdict(list)
    total_cost = 0.0
    total_duration_ms = 0
    error_count = 0

    for row in rows:
        by_session[row.session_id].append(row)
        total_cost += row.cost
        total_duration_ms += row.duration_ms or 0
        if row.error:
            error_count += 1

    for session_id, items in by_session.items():
        sessions.append(
            {
                "session_id": session_id,
                "message_count": len(items),
                "first_message": items[0].timestamp,
                "last_message": items[-1].timestamp,
                "project_path": items[0].project_path,
            }
        )

    return {
        "user_id": rows[0].user_id,
        "username": rows[0].username,
        "message_count": len(rows),
        "session_count": len(by_session),
        "first_message": rows[0].timestamp,
        "last_message": rows[-1].timestamp,
        "total_cost": round(total_cost, 6),
        "total_duration_ms": total_duration_ms,
        "error_count": error_count,
        "sessions": sessions,
    }


def markdown_code_block(text: str) -> str:
    return f"```\n{text.rstrip()}\n```" if text.strip() else "_(vacío)_"


def render_markdown(
    title: str, bot_handle: str, summary: dict[str, object], rows: list[ChatRow]
) -> str:
    lines = [
        f"# {title}",
        "",
        f"- Bot: `{bot_handle}`",
        f"- User ID: `{summary['user_id']}`",
        f"- Username: `{summary['username'] or 'N/A'}`",
        f"- Sessions: `{summary['session_count']}`",
        f"- Turns stored: `{summary['message_count']}`",
        f"- First message: `{iso_to_local(summary['first_message'])}`",
        f"- Last message: `{iso_to_local(summary['last_message'])}`",
        f"- Errors: `{summary['error_count']}`",
        f"- Estimated total runtime: `{summary['total_duration_ms']} ms`",
        f"- Total tracked cost: `{summary['total_cost']}`",
        "",
        "## Sessions",
        "",
    ]

    for session in summary["sessions"]:
        lines.append(
            f"- `{session['session_id']}` | {session['message_count']} turns | "
            f"{iso_to_local(session['first_message'])} -> "
            f"{iso_to_local(session['last_message'])} | "
            f"`{session['project_path']}`"
        )

    lines.extend(["", "## Conversation", ""])

    for row in rows:
        lines.extend(
            [
                f"### Turn {row.message_id}",
                "",
                f"- Session: `{row.session_id}`",
                f"- Timestamp: `{iso_to_local(row.timestamp)}`",
                f"- Project: `{row.project_path}`",
                f"- Cost: `{row.cost}`",
                f"- Duration: `{row.duration_ms or 0} ms`",
                "",
                "**User**",
                "",
                markdown_code_block(row.prompt),
                "",
                f"**{bot_handle}**",
                "",
                markdown_code_block(row.response),
                "",
            ]
        )
        if row.error:
            lines.extend(["**Error**", "", markdown_code_block(row.error), ""])

    return "\n".join(lines).rstrip() + "\n"


def paragraphize(text: str) -> str:
    chunks = [escape(part) for part in text.strip().splitlines()]
    if not chunks:
        return "<p class='empty'>(vacío)</p>"
    return "".join(f"<p>{line or '&nbsp;'}</p>" for line in chunks)


def render_html(
    title: str, bot_handle: str, summary: dict[str, object], rows: list[ChatRow]
) -> str:
    session_items = "\n".join(
        (
            "<li>"
            f"<code>{escape(session['session_id'])}</code> | {session['message_count']} turns | "
            f"{escape(iso_to_local(session['first_message']))} -> "
            f"{escape(iso_to_local(session['last_message']))} | "
            f"<code>{escape(session['project_path'])}</code>"
            "</li>"
        )
        for session in summary["sessions"]
    )

    turns = []
    for row in rows:
        error_block = ""
        if row.error:
            error_block = (
                "<div class='bubble error'>"
                "<div class='label'>Error</div>"
                f"{paragraphize(row.error)}"
                "</div>"
            )
        turns.append(
            f"""
            <section class="turn">
              <div class="turn-meta">
                <span>Turn {row.message_id}</span>
                <span>{escape(iso_to_local(row.timestamp))}</span>
                <span><code>{escape(row.session_id)}</code></span>
              </div>
              <div class="turn-project"><code>{escape(row.project_path)}</code></div>
              <div class="bubble user">
                <div class="label">User</div>
                {paragraphize(row.prompt)}
              </div>
              <div class="bubble bot">
                <div class="label">{escape(bot_handle)}</div>
                {paragraphize(row.response)}
              </div>
              {error_block}
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --paper: #fffdf9;
      --ink: #1f2933;
      --muted: #52606d;
      --user: #d9f99d;
      --bot: #dbeafe;
      --error: #fee2e2;
      --border: #d9d4cb;
      --accent: #8b5e34;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #efe7da 0%, var(--bg) 240px);
      color: var(--ink);
      font: 15px/1.5 "Georgia", "Times New Roman", serif;
    }}
    .page {{
      max-width: 980px;
      margin: 0 auto;
      padding: 40px 28px 64px;
    }}
    .card {{
      background: var(--paper);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 18px 60px rgba(55, 40, 20, 0.08);
      padding: 28px;
      margin-bottom: 24px;
    }}
    h1, h2 {{
      margin: 0 0 14px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    h1 {{
      font-size: 34px;
      color: #2d1f12;
    }}
    h2 {{
      font-size: 22px;
      color: var(--accent);
      margin-top: 8px;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px 18px;
      margin-top: 16px;
    }}
    .meta-grid div {{
      padding: 10px 12px;
      background: #faf6ef;
      border-radius: 10px;
      border: 1px solid #ece4d7;
    }}
    .meta-grid strong {{
      display: block;
      font-size: 12px;
      text-transform: uppercase;
      color: var(--muted);
      letter-spacing: 0.08em;
      margin-bottom: 4px;
    }}
    ul {{
      margin: 0;
      padding-left: 20px;
    }}
    code {{
      font-family: "SFMono-Regular", "Menlo", monospace;
      font-size: 0.9em;
    }}
    .turn {{
      padding: 18px 0 24px;
      border-top: 1px solid #ece4d7;
      page-break-inside: avoid;
    }}
    .turn:first-of-type {{
      border-top: none;
      padding-top: 0;
    }}
    .turn-meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .turn-project {{
      margin-bottom: 14px;
      color: var(--muted);
    }}
    .bubble {{
      border-radius: 16px;
      padding: 14px 16px;
      margin: 10px 0;
      border: 1px solid rgba(0,0,0,0.06);
    }}
    .bubble.user {{ background: var(--user); }}
    .bubble.bot {{ background: var(--bot); }}
    .bubble.error {{ background: var(--error); }}
    .label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #334155;
      margin-bottom: 6px;
      font-weight: 700;
    }}
    p {{
      margin: 0 0 8px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .empty {{
      color: var(--muted);
      font-style: italic;
    }}
    @page {{
      size: A4;
      margin: 14mm;
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="card">
      <h1>{escape(title)}</h1>
      <div>Bot: <strong>{escape(bot_handle)}</strong></div>
      <div class="meta-grid">
        <div><strong>User ID</strong>{summary['user_id']}</div>
        <div><strong>Username</strong>{escape(summary['username'] or 'N/A')}</div>
        <div><strong>Sessions</strong>{summary['session_count']}</div>
        <div><strong>Turns stored</strong>{summary['message_count']}</div>
        <div><strong>First message</strong>{escape(iso_to_local(summary['first_message']))}</div>
        <div><strong>Last message</strong>{escape(iso_to_local(summary['last_message']))}</div>
        <div><strong>Errors</strong>{summary['error_count']}</div>
        <div><strong>Total tracked cost</strong>{summary['total_cost']}</div>
      </div>
    </section>
    <section class="card">
      <h2>Sessions</h2>
      <ul>{session_items}</ul>
    </section>
    <section class="card">
      <h2>Conversation</h2>
      {''.join(turns)}
    </section>
  </main>
</body>
</html>
"""


def render_json(
    title: str, bot_handle: str, summary: dict[str, object], rows: list[ChatRow]
) -> str:
    payload = {
        "title": title,
        "bot_handle": bot_handle,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "messages": [
            {
                "message_id": row.message_id,
                "session_id": row.session_id,
                "user_id": row.user_id,
                "username": row.username,
                "project_path": row.project_path,
                "timestamp": row.timestamp,
                "prompt": row.prompt,
                "response": row.response,
                "cost": row.cost,
                "duration_ms": row.duration_ms,
                "error": row.error,
            }
            for row in rows
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def find_chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def print_pdf(html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    chrome = find_chrome()
    if chrome is None:
        return False, "No Chrome-compatible browser found for headless PDF export."

    with tempfile.TemporaryDirectory(prefix="chat-export-") as tmp_dir:
        cmd = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            f"--user-data-dir={tmp_dir}",
            f"--print-to-pdf={pdf_path}",
            html_path.resolve().as_uri(),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if proc.returncode != 0 or not pdf_path.exists():
        details = (proc.stderr or proc.stdout).strip() or "Unknown Chrome failure."
        return False, details
    return True, "PDF generated with headless Chrome."


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    try:
        user_id = args.user_id if args.user_id is not None else pick_default_user(conn)
        rows = fetch_rows(conn, user_id, args.session_ids)
    finally:
        conn.close()

    summary = build_summary(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"rudagency_bot_chat_user_{summary['user_id']}_{stamp}"

    markdown = render_markdown(args.title, args.bot_handle, summary, rows)
    html = render_html(args.title, args.bot_handle, summary, rows)
    json_text = render_json(args.title, args.bot_handle, summary, rows)

    md_path = args.output_dir / f"{base_name}.md"
    html_path = args.output_dir / f"{base_name}.html"
    json_path = args.output_dir / f"{base_name}.json"
    pdf_path = args.output_dir / f"{base_name}.pdf"

    write_text(md_path, markdown)
    write_text(html_path, html)
    write_text(json_path, json_text)

    pdf_ok = False
    pdf_status = "PDF export skipped."
    if not args.skip_pdf:
        pdf_ok, pdf_status = print_pdf(html_path, pdf_path)

    result = {
        "user_id": summary["user_id"],
        "message_count": summary["message_count"],
        "session_count": summary["session_count"],
        "markdown": str(md_path),
        "html": str(html_path),
        "json": str(json_path),
        "pdf": str(pdf_path) if pdf_ok else None,
        "pdf_status": pdf_status,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if pdf_ok or args.skip_pdf else 1


if __name__ == "__main__":
    sys.exit(main())
