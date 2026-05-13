# Contributing to AURA

AURA is a personal agent system built and improved collaboratively between Ricardo and Claude. The codebase evolves through the proactive conductor loop — most improvements are made by the system itself. External contributions should align with this philosophy.

## Development Status

| Module | Status |
|---|---|
| Brain routing (Haiku primary, cascade) | Production-stable |
| RAG memory (Obsidian + semantic search) | Active |
| Knowledge pipeline (DuckDB) | Active |
| Proactive conductor loop | Production-stable |
| Dashboard (FastAPI + SSE) | Beta |
| Tool manifest (live port checks) | Active |
| Social media pipeline | Beta |
| Voice (TTS + transcription) | Beta |
| Hermes mesh bridge | Active |

## Getting Started

**Requirements:**
- Python 3.11+ (tested on 3.11–3.13)
- `uv` for dependency management
- `claude` CLI authenticated via Anthropic subscription
- Ollama with `nomic-embed-text` (for RAG embeddings)

```bash
git clone https://github.com/royaluniondesign-sys/claude-code-telegram
cd claude-code-telegram

uv install         # install deps
cp .env.example .env
# fill TELEGRAM_BOT_TOKEN, APPROVED_DIRECTORY, ALLOWED_USERS

uv run make test   # 498 tests, should all pass
uv run make lint   # black + isort + flake8 + mypy
```

## Architecture Principles

Before contributing, understand the design:

1. **No per-message API cost** — AURA drives `claude` CLI via subprocess. Never use the Anthropic SDK directly.
2. **Haiku-first** — `Intent.CHAT` and `Intent.TRANSLATE` route to Haiku. Sonnet only for complexity pressure.
3. **RAG-injected context** — `build_system_prompt_async(user_message)` must be called before every LLM invoke so Obsidian context is included.
4. **Tool manifest is live** — `build_tool_manifest()` checks actual TCP ports. Don't hardcode service availability.
5. **Protected core files** — the nine files in `_PROTECTED_CORE_FILES` must not be modified by the conductor. Respect this denylist.
6. **Dead code = noise** — if a module has zero imports and no documented future use, delete it.

## Code Standards

**Type hints everywhere:**
```python
async def process(items: list[dict[str, Any]], config: Path | None = None) -> bool:
    ...
```

**Structured logging (not print):**
```python
import structlog
logger = structlog.get_logger()
logger.info("operation_done", user_id=user_id, elapsed_ms=elapsed)
```

**Immutable patterns:**
```python
# Wrong — mutates existing dict
config["key"] = value

# Right — returns new dict
new_config = {**config, "key": value}
```

**Error handling at boundaries:**
```python
# Validate at Telegram input boundary
# Trust internal module calls
# Never silently swallow exceptions — log them
```

**File size:** 200–400 lines is typical, 800 max. Extract when files grow beyond that.

## Testing

```bash
uv run make test         # full suite with coverage
pytest tests/unit/ -q    # unit tests only
pytest -k test_name -v   # single test
```

- Minimum 80% coverage on new modules
- Mock at the RAG layer (`RAGIndexer`, `RAGRetriever`) when testing modules that use memory — don't require Ollama in CI
- Use `pytest-asyncio` with `asyncio_mode = "auto"` (already configured)

## Brain / Routing Changes

If modifying `src/brains/router.py`:
- `Intent.CHAT` and `Intent.TRANSLATE` must stay on `"haiku"`
- `Intent.SHELL` must stay on `"zero-token"` (no LLM for bash)
- Pressure fallback to `"openrouter"` is intentional — don't remove
- Run `pytest tests/unit/test_brain_router.py -v` after changes

If adding a new brain:
- Implement `async execute(prompt, context) -> str`
- Add to `_INTENT_BRAIN_MAP` only for intents it handles better than existing brains
- Add a `src/brains/test_yourname_brain.py` unit test with mocked subprocess

## RAG / Memory Changes

If modifying `src/rag/`:
- `indexer.py` uses content-hash deduplication — don't break this or re-indexing will explode costs
- `INDEX_SOURCES` in `indexer.py` controls what gets indexed — add carefully, Obsidian vault is ~7,000 files
- `retriever.py` returns results sorted by cosine similarity — don't change the sort order
- `store.py` SQLite schema at `~/.aura/rag.db` — add migration if changing schema

## Knowledge Pipeline Changes

If modifying `src/spark/pipeline.py`:
- Must run without Java/JVM (DuckDB only)
- Must not write to `rag.db` — read-only from the store
- Output must go to `~/.aura/knowledge_lake/` as Parquet
- `--dry-run` flag must always work and produce no writes

## Commit Format

```
feat: add knowledge lake scheduler (runs pipeline every 6h)
fix: haiku brain --verbose flag missing from execute_streaming
refactor: extract tool manifest into separate module
docs: update brain cascade table in README
test: add RAG indexer smoke test with mocked Ollama
chore: bump duckdb to 1.5.2
```

## Pull Request Process

1. `uv run make test` — all 498 must pass
2. `uv run make lint` — no new warnings
3. PR description: what changed, why, how to test it
4. Reference any issue or Roadmap item from README

## Security Guidelines

- Never commit `.env`, tokens, keys, or credentials
- Never use `git add -A` in scripts — always stage explicit files
- Don't modify `_PROTECTED_CORE_FILES` from the conductor
- Webhook handlers must verify signatures before processing
- All user input passes through `SecurityValidator` before reaching Claude

Report security issues privately — do not open public GitHub issues. See [SECURITY.md](SECURITY.md).

## Getting Help

- `docs/` directory has architecture and setup guides
- `CLAUDE.md` has AURA-specific system behavior documentation
- `~/.aura/memory/self-awareness.md` has the map of danger zones in the codebase
- Run `uv run make run-debug` for detailed structured logs
