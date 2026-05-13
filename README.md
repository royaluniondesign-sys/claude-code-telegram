<p align="center">
  <img src="https://img.shields.io/badge/AURA-v0.11.0-d97757?style=for-the-badge&labelColor=0e0d0c&color=d97757" alt="AURA v0.11.0">
  <img src="https://img.shields.io/badge/Python-3.11+-3776ab?style=for-the-badge&labelColor=0e0d0c" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Tests-498_passing-22c55e?style=for-the-badge&labelColor=0e0d0c" alt="Tests: 498 passing">
  <img src="https://img.shields.io/badge/Brain-Haiku_Primary-7c5cff?style=for-the-badge&labelColor=0e0d0c" alt="Haiku Primary">
  <img src="https://img.shields.io/badge/RAG-11k%2B_chunks-f59e0b?style=for-the-badge&labelColor=0e0d0c" alt="RAG: 11k+ chunks">
  <img src="https://img.shields.io/badge/License-MIT-64748b?style=for-the-badge&labelColor=0e0d0c" alt="MIT">
</p>

```
 █████╗ ██╗   ██╗██████╗  █████╗
██╔══██╗██║   ██║██╔══██╗██╔══██╗
███████║██║   ██║██████╔╝███████║
██╔══██║██║   ██║██╔══██╗██╔══██║
██║  ██║╚██████╔╝██║  ██║██║  ██║
╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
Autonomous Unified Reasoning Agent
```

**A personal AI agent that lives on your Mac, runs 24/7, and does things Claude can't do alone.**

AURA runs as a macOS LaunchAgent, accessible from anywhere via Telegram. It routes every request to the cheapest capable brain, executes tasks autonomously, maintains persistent semantic memory over your Obsidian vault, generates images, publishes to Instagram, and continuously improves its own codebase — all without touching an API key or paying per message.

No SDK. No per-message billing. Your hardware, your credentials, your data.

---

## What is AURA

AURA is a self-directing AI agent accessible from your phone via Telegram. You send a message — in any language — and AURA decides how to handle it: run a bash command directly, pull semantic context from your Obsidian vault, delegate to a free local model, escalate to Claude Haiku for complex tasks, or dispatch a background job and notify you when done.

The key distinctions from every other AI assistant setup:

- **No Anthropic SDK** — AURA drives the `claude` CLI via subprocess. Zero per-message API billing. Your Claude subscription covers everything.
- **Haiku-first brain cascade** — 11 brains in cost order. Most chat, translation, and general tasks go to Claude Haiku (subscription, no extra cost, fast). Claude Sonnet only when complexity demands it.
- **Obsidian-aware memory** — your entire Obsidian vault is indexed (11k+ chunks) with semantic search. Every response is enriched with relevant context from your own notes.
- **Tool-aware routing** — AURA checks what services are running on your Mac right now (open-design, ComfyUI, Instagram pipeline) and uses them without you spelling it out.
- **Proactive conductor** — every 15 minutes, AURA analyzes its own state, generates improvement tasks, and executes them. No prompting required.
- **Knowledge pipeline** — DuckDB analytics over all indexed content. Queryable Parquet tables at `~/.aura/knowledge_lake/`.

---

## Why AURA — What Claude alone can't do

| Capability | Claude chat | AURA |
|---|---|---|
| 24/7 operation | No | Yes — LaunchAgent, always running |
| Obsidian vault memory | No | Yes — 11k+ chunks, semantic search |
| Scheduled tasks | No | Yes — APScheduler cron jobs |
| Local filesystem access | No | Yes — read/write any file on your Mac |
| Image generation (FLUX.1) | No | Yes — ComfyUI local or Pollinations.ai |
| Instagram publishing | No | Yes — Meta Graph API, one natural command |
| Design generation | No | Yes — open-design carousels and posts |
| Proactive self-improvement | No | Yes — conductor loop, every 15 min |
| Cost routing (free first) | No | Yes — 11 brains, $0 for most tasks |
| Voice messages (TTS) | No | Yes — edge-tts |
| Self-healing on failure | No | Yes — watchdog + auto-restart |
| Knowledge analytics | No | Yes — DuckDB pipeline over all memory |

---

## Architecture — The Intelligence Stack

```
Layer 0: LaunchAgent (macOS, KeepAlive=true, 10s ThrottleInterval)
         ↓
Layer 1: Python bot process (src/main.py)
         ↓
Layer 1.5: Watchdog ping loop (2-min interval, 3-strike SIGTERM → restart)
         ↓
Layer 2: Conductor / Proactive Loop — 15-min autonomous task cycle
         ↓
Layer 3: Brain Router → AuraCortex (EMA scoring) → cheapest capable brain
         ┌──────────────┬──────────────────┬───────────────────┐
         │  zero-token  │  free-tier brains│  subscription     │
         │ bash/git/ops │ qwen/gemini/ollama│ haiku → sonnet    │
         └──────────────┴──────────────────┴───────────────────┘
         ↓
Layer 4: RAG Context Injection (build_system_prompt_async)
         ├── Obsidian vault ~/Obsidian/**/*.md (~11k chunks)
         ├── ~/.aura/memory/ markdown files
         ├── MISSION.md + CLAUDE.md (identity + mission)
         ├── src/**/*.py (own source code)
         └── Telegram conversation history
         ↓
Layer 4.5: Tool Manifest (live TCP port checks at call time)
         ├── open-design :59826 → carousel/post generation
         ├── ComfyUI :8188 → FLUX.1-dev local images
         └── Termora :4030 → interactive mobile terminal
         ↓
Layer 5: Knowledge Pipeline (~/.aura/knowledge_lake/)
         └── DuckDB: keywords | source_summary | recent_memory | conversations
         ↓
Layer 6: FastAPI :8080 + Dashboard (SSE real-time panels)
```

**Supporting systems running in parallel:**
- `AutoExecutor` — picks up pending tasks every 5 minutes
- `SelfEvaluator` — scans codebase every 30 minutes, auto-creates fix tasks
- `EventBus` — async pub/sub: webhooks → agent → notifications
- `RAGIndexer` — background re-index every 5 minutes (content-hash, only re-embeds changes)

---

## Brain Cascade — Cost-Optimized Routing

| Brain | Cost | Primary use |
|---|---|---|
| `zero-token` | $0 | Bash, git, file ops — no LLM, instant |
| `api-zero` | $0 | Weather, crypto, QR codes via free public APIs |
| `ollama-rud` | $0 | Remote LAN Ollama server, code-focused |
| `qwen-code` | $0 | Alibaba Qwen Code CLI, 1000 req/day |
| `opencode` | $0 | OpenCode CLI + OpenRouter backend |
| `gemini` | $0 | Google Gemini CLI, free tier, web-aware |
| `openrouter` | $0 | OpenRouter free model (pressure fallback only) |
| `cline` | $0 | Local Ollama via Cline |
| **`haiku`** | subscription | **Primary brain** — chat, translate, general tasks |
| `sonnet` | subscription | Complex reasoning, architecture, long tasks |
| `opus` | subscription | Deepest reasoning, maximum quality |

**Intent routing (v0.11.0):**

| Intent | Brain |
|---|---|
| `CHAT`, `TRANSLATE` | **Haiku** (was OpenRouter — fixed in v0.11.0) |
| `CODE` | ollama-rud → qwen → haiku cascade |
| `SEARCH` | Gemini |
| `SHELL` | zero-token (no LLM) |
| `IMAGE` | image-brain (ComfyUI / Pollinations) |
| `DESIGN` | open-design tool |
| `SOCIAL` | instagram_publish / social pipeline |

**Pressure-aware fallback:** Haiku usage ≥ 70% of rate limit → auto-fallback to OpenRouter free tier.

**AuraCortex:** self-learning EMA layer above the router. Tracks success rate and latency per brain per intent. Creates bypass rules after 2+ failures. All state persists to `~/.aura/cortex.json`.

---

## RAG Memory — Obsidian + Semantic Search

Every brain call is enriched with the most relevant 1,500 chars of context from your entire knowledge base — automatically, without any prompting.

- **Embeddings**: `nomic-embed-text` via Ollama, 768-dim, fully local
- **Storage**: SQLite at `~/.aura/rag.db` — 11,000+ chunks
- **Sources indexed**:
  - `~/Obsidian/**/*.md` — entire Obsidian vault
  - `~/.aura/memory/*.md` — AURA persistent memory
  - `MISSION.md`, `CLAUDE.md` — identity and mission
  - `src/**/*.py` — AURA source code
  - Bot logs (last 500 lines, rolling)
  - Telegram conversation history (indexed after each exchange)
- **Auto re-index**: every 5 minutes, content-hash based (skips unchanged chunks)
- **Context injection**: `build_system_prompt_async(user_message)` runs semantic search before every LLM call

---

## Knowledge Pipeline — DuckDB Analytics

```bash
# Count chunks by type (no writes)
uv run python -m src.spark.pipeline --dry-run

# Build all Parquet tables (~3 seconds for 11k chunks)
uv run python -m src.spark.pipeline

# Query top keywords from memory
uv run python -m src.spark.pipeline --query keywords --top 20 --type memory

# Query most active sources
uv run python -m src.spark.pipeline --query source_summary --top 10
```

**Tables at `~/.aura/knowledge_lake/`:**

| Table | Content |
|---|---|
| `keywords.parquet` | Word frequency by source_type |
| `source_summary.parquet` | Chunk count + chars + last updated per source |
| `recent_memory.parquet` | Latest 200 Obsidian + memory chunks |
| `conversations.parquet` | All indexed Telegram exchanges |

Engine: DuckDB (no Java/JVM, ~3s). Architecture is PySpark-compatible for cluster scale.

---

## Tool Manifest — Live Service Detection

Every brain call includes a dynamically generated section describing what's actually running right now. Detected via TCP port check at call time — no stale config.

| Service | Port | AURA action |
|---|---|---|
| open-design | 59826 | Generates carousels, posts with RUD branding |
| ComfyUI (FLUX.1-dev) | 8188 | Photorealistic images in ~90s |
| Termora terminal | 4030 | One-click mobile terminal URL |

When you say "haz un carousel para Instagram sobre X" — AURA checks open-design, calls it, returns the result. No explicit instructions needed.

---

## Security Model

**Five-layer defense:**

1. **Authentication** — Telegram user ID whitelist. Only configured users can interact.
2. **Directory isolation** — all file ops sandboxed to `APPROVED_DIRECTORY`. Path traversal blocked.
3. **Input validation** — blocks `;`, `&&`, `$()`, backticks, shell injection. Secrets files blocked.
4. **Rate limiting** — per-user token bucket.
5. **Audit logging** — every action recorded in SQLite.

**Autonomous loop protection:**

The conductor can modify any source file, but nine core engine files are in a `frozenset` denylist — never auto-staged, never auto-committed:

```python
_PROTECTED_CORE_FILES = frozenset({
    "src/infra/proactive_loop.py",
    "src/infra/watchdog.py",
    "src/main.py",
    "src/config/settings.py",
    "src/config/features.py",
    "src/brains/conductor.py",
    "src/brains/router.py",
    "src/mcp/cli_registrar.py",
    "src/bot/orchestrator.py",
})
```

**Additional safeguards:**
- Conductor never runs `git add -A` — stages only explicit files
- After every conductor commit: pytest runs, auto-revert on failure
- Secret file filter: `.env`, `credential`, `token`, `password`, `private_key` always skipped
- Brain subprocesses isolated — one brain failing doesn't cascade
- Webhook HMAC-SHA256 (GitHub) + Bearer token (generic) + replay deduplication

See [SECURITY.md](SECURITY.md) for full threat model, configuration, and production checklist.

---

## Installation

**Requirements:** Python 3.11+, `uv`, `claude` CLI authenticated, Ollama with `nomic-embed-text`.

```bash
git clone https://github.com/royaluniondesign-sys/claude-code-telegram
cd claude-code-telegram
uv install

cp .env.example .env
# Minimum required:
# TELEGRAM_BOT_TOKEN=...
# APPROVED_DIRECTORY=/Users/yourname
# ALLOWED_USERS=your-telegram-id

# Embeddings (RAG)
brew install ollama
ollama pull nomic-embed-text

# Install as LaunchAgent
cp src/infra/com.aura.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aura.bot.plist
```

**Recommended `.env` additions:**
```bash
AGENTIC_MODE=true
ENABLE_API_SERVER=true
API_SERVER_PORT=8080
NOTIFICATION_CHAT_IDS=your-telegram-id
GEMINI_ENABLED=true
OPENROUTER_API_KEY=sk-or-...
```

**Dev commands:**
```bash
uv run make dev          # install all deps (including dev)
uv run make run          # run the bot
uv run make run-debug    # debug logging
uv run make test         # 498 tests with coverage
uv run make lint         # black + isort + flake8 + mypy
uv run make format       # auto-format
```

---

## Project Layout

```
src/
├── brains/           Brain implementations + Cortex + router
│   ├── cortex.py     EMA self-learning routing layer
│   ├── router.py     Intent → brain map (Haiku primary, v0.11.0)
│   ├── conductor.py  3-layer autonomous loop (analysis → impl → verify)
│   └── claude_brain.py  Claude CLI subprocess + async RAG context injection
├── context/          System prompt construction
│   ├── aura_context.py      build_system_prompt_async() — RAG + manifest + memory
│   └── mempalace_memory.py  RAG-backed conversation memory (stub → RAG)
├── rag/              Local vector search
│   ├── embedder.py   Async Ollama embedder + LRU cache
│   ├── indexer.py    Incremental indexer (Obsidian-aware, content-hash)
│   ├── retriever.py  Cosine similarity + context formatter
│   └── store.py      SQLite vector store (~/.aura/rag.db)
├── spark/            Knowledge analytics
│   └── pipeline.py   DuckDB pipeline → ~/.aura/knowledge_lake/ Parquet
├── economy/          Intent classification (regex + semantic)
├── infra/            Runtime: proactive loop, watchdog, auto executor, LaunchAgent
├── scheduler/        APScheduler cron + routines store
├── bot/              Telegram handlers, middleware, orchestrator
├── claude/           Claude CLI facade, session management
├── api/              FastAPI server, webhooks, dashboard routes
├── storage/          SQLite repositories (sessions, audit, costs)
├── security/         Auth, input validation, rate limiting, audit
├── events/           Async pub/sub EventBus
├── notifications/    Rate-limited Telegram delivery
└── voice/            TTS, voice daemon, screen/computer control tools
dashboard/            Single-file dashboard UI (SSE real-time, 10 panels)
scripts/              rud_server_setup.sh, export_chat_history.py
```

**Key data paths:**
```
~/.aura/rag.db                SQLite vector store (11k+ chunks)
~/.aura/knowledge_lake/       DuckDB Parquet analytics tables
~/.aura/brain/memory.md       Persistent AURA memory (facts, projects, rules)
~/.aura/brain/identity.md     AURA identity and persona
~/.aura/cortex.json           Self-learned brain routing scores + bypass rules
~/Obsidian/                   Obsidian vault (fully indexed, primary memory source)
```

---

## Roadmap — What's Next

| Feature | Status | Notes |
|---|---|---|
| **Hermes RAG bridge** | Planned | Hermes gets same Obsidian context via mesh |
| **Knowledge lake scheduler** | Planned | Run pipeline automatically every 6h |
| **Dashboard knowledge panel** | Planned | Surface knowledge_lake queries in UI |
| **Wan2.1 video pipeline** | Planned | Short-form video for Reels/TikTok |
| **mem0 vector store** | Planned | Replace SQLite cosine search with mem0 |
| **Spark cluster mode** | Future | PySpark when knowledge_lake > 1M chunks |
| **Credential rotation** | Ongoing | Rotate all tokens and API keys regularly |
| **Hermes tool reduction** | Planned | Reduce active tools for better latency |

---

## Tech Stack

| Component | Library / Version |
|---|---|
| Language | Python 3.11–3.13 |
| Telegram | python-telegram-bot 22.x |
| API server | FastAPI + uvicorn |
| Scheduler | APScheduler |
| Database | SQLite + aiosqlite |
| Embeddings | Ollama nomic-embed-text (768-dim) |
| Vector math | numpy cosine similarity |
| Analytics | DuckDB 1.5.x (knowledge pipeline) |
| Logging | structlog (JSON prod / console dev) |
| Deps | uv |
| Claude interface | `claude` CLI subprocess (subscription auth, no SDK) |

---

## Project Status

- **Version**: 0.11.0
- **Tests**: 498 passing, 23% coverage
- **Primary brain**: Claude Haiku (subscription, no extra cost)
- **RAG**: Active — 11,008 chunks (Obsidian + memory + code + logs)
- **Knowledge lake**: Active — 4 Parquet tables, ~3s build time
- **Stability**: Beta — core routing, autonomous loop, RAG are production-stable

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <strong>AURA — your hardware, your models, your rules.</strong><br>
  <em>Built autonomously, improved continuously.</em>
</p>
