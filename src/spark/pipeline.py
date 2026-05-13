"""AURA Knowledge Pipeline — DuckDB-backed analytics.

Uses DuckDB for in-process SQL analytics (no Java/JVM required).
Same architecture as Spark: read from source, transform, write Parquet.
Can be migrated to PySpark cluster if scale demands it.

Reads chunks from ~/.aura/rag.db, writes Parquet to ~/.aura/knowledge_lake/.

Run:
    uv run python -m src.spark.pipeline
    uv run python -m src.spark.pipeline --dry-run
    uv run python -m src.spark.pipeline --query keywords --top 20
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_RAG_DB = Path.home() / ".aura" / "rag.db"
_LAKE_DIR = Path.home() / ".aura" / "knowledge_lake"


def _get_duckdb():
    import duckdb
    con = duckdb.connect()
    con.execute("SET threads=4; SET memory_limit='512MB'")
    return con


def _load_rag_to_duckdb(con) -> int:
    """Attach rag.db and expose rag_chunks as a relation."""
    sqlite_conn = sqlite3.connect(str(_RAG_DB))
    rows = sqlite_conn.execute(
        "SELECT id, source, source_type, content, metadata, updated_at FROM rag_chunks"
    ).fetchall()
    sqlite_conn.close()

    con.execute("""
        CREATE OR REPLACE TABLE chunks AS
        SELECT
            col0 AS id,
            col1 AS source,
            col2 AS source_type,
            col3 AS content,
            col4 AS metadata,
            col5 AS updated_at
        FROM (VALUES """ + ",".join(f"(?,?,?,?,?,?)" for _ in rows) + ")",
        [val for row in rows for val in row],
    )
    return len(rows)


def run(dry_run: bool = False) -> dict[str, Any]:
    """Execute the knowledge pipeline. Returns stats dict."""
    if not _RAG_DB.exists():
        logger.error("rag_db_missing", path=str(_RAG_DB))
        return {"error": "rag.db not found"}

    _LAKE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("pipeline_start", engine="duckdb", dry_run=dry_run)

    con = _get_duckdb()
    total = _load_rag_to_duckdb(con)
    logger.info("chunks_loaded", total=total)

    if dry_run:
        counts = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT source_type, count(*) FROM chunks GROUP BY source_type ORDER BY 2 DESC"
            ).fetchall()
        }
        logger.info("dry_run_counts", **counts)
        return {"dry_run": True, "total_chunks": total, "by_type": counts}

    # ── Keyword frequency ─────────────────────────────────────────────────────
    kw_path = str(_LAKE_DIR / "keywords.parquet")
    con.execute(f"""
        COPY (
            SELECT source_type, word, count(*) AS freq
            FROM (
                SELECT source_type, unnest(string_split(lower(content), ' ')) AS word
                FROM chunks
            )
            WHERE length(word) > 4
              AND NOT regexp_matches(word, '^[0-9]+$')
              AND NOT regexp_matches(word, '^[^a-záéíóúñ]+$')
            GROUP BY source_type, word
            HAVING count(*) > 3
            ORDER BY freq DESC
        ) TO '{kw_path}' (FORMAT PARQUET)
    """)
    logger.info("keywords_written", path=kw_path)

    # ── Source summary ────────────────────────────────────────────────────────
    src_path = str(_LAKE_DIR / "source_summary.parquet")
    con.execute(f"""
        COPY (
            SELECT
                source,
                source_type,
                count(*) AS chunk_count,
                sum(length(content)) AS total_chars,
                max(updated_at) AS last_updated
            FROM chunks
            GROUP BY source, source_type
            ORDER BY chunk_count DESC
        ) TO '{src_path}' (FORMAT PARQUET)
    """)
    logger.info("source_summary_written", path=src_path)

    # ── Recent memory chunks ──────────────────────────────────────────────────
    mem_path = str(_LAKE_DIR / "recent_memory.parquet")
    con.execute(f"""
        COPY (
            SELECT source, content, updated_at
            FROM chunks
            WHERE source_type IN ('memory', 'mission')
            ORDER BY updated_at DESC
            LIMIT 200
        ) TO '{mem_path}' (FORMAT PARQUET)
    """)
    logger.info("recent_memory_written", path=mem_path)

    # ── Conversation extracts (telegram_chat only) ────────────────────────────
    conv_path = str(_LAKE_DIR / "conversations.parquet")
    con.execute(f"""
        COPY (
            SELECT id, content, updated_at
            FROM chunks
            WHERE source = 'telegram_chat'
            ORDER BY updated_at DESC
        ) TO '{conv_path}' (FORMAT PARQUET)
    """)
    logger.info("conversations_written", path=conv_path)

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "engine": "duckdb",
        "total_chunks": total,
        "tables": ["keywords", "source_summary", "recent_memory", "conversations"],
    }
    (_LAKE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    logger.info("pipeline_done", lake=str(_LAKE_DIR))
    con.close()
    return manifest


def query(table: str, top_n: int = 20, source_type: str | None = None) -> list[dict]:
    """Read a knowledge table and return top N rows as dicts."""
    import duckdb

    parquet_path = _LAKE_DIR / f"{table}.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(f"Run pipeline first: {parquet_path} not found")

    con = duckdb.connect()
    where = f"WHERE source_type = '{source_type}'" if source_type else ""
    rows = con.execute(
        f"SELECT * FROM read_parquet('{parquet_path}') {where} LIMIT {top_n}"
    ).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, row)) for row in rows]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="AURA Knowledge Pipeline")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--query", metavar="TABLE", help="Query a knowledge table (after run)")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--type", dest="source_type", default=None)
    args = parser.parse_args()

    if args.query:
        rows = query(args.query, top_n=args.top, source_type=args.source_type)
        print(json.dumps(rows, indent=2, default=str))
    else:
        result = run(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
