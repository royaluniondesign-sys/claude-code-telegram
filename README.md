<p align="center">
  <img src="https://img.shields.io/badge/AURA-v0.10.0-d97757?style=for-the-badge&labelColor=0e0d0c&color=d97757" alt="AURA">
  <img src="https://img.shields.io/badge/Python-3.11+-3776ab?style=for-the-badge&labelColor=0e0d0c" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Tests-491_passing-22c55e?style=for-the-badge&labelColor=0e0d0c" alt="Tests: 491 passing">
  <img src="https://img.shields.io/badge/License-MIT-7c5cff?style=for-the-badge&labelColor=0e0d0c" alt="MIT">
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

AURA is a self-directing AI agent accessible from your phone via Telegram. It routes every request to the cheapest brain capable of handling it, executes shell commands without touching an LLM, runs scheduled routines while you sleep, and continuously improves its own codebase through an autonomous conductor loop.

No SDK. No per-message API billing. Your hardware, your credentials, your data.

---

## 🤖 What is AURA

AURA is a personal AI agent that runs as a macOS LaunchAgent — always on, always reachable. You send a message from Telegram (or Telegram Web, from any device), and AURA decides how to handle it: run a bash command directly, delegate to a free local model, escalate to Claude if the task genuinely needs it, or dispatch a background task and notify you when it's done.

The key distinction from every other "AI assistant" setup:

- **No Anthropic SDK** — AURA drives the `claude` CLI via subprocess. Zero per-message API billing. Your Claude subscription covers everything Claude does.
- **Multi-brain cascade** — 11 brains in cost order. The router picks the cheapest capable brain per task. Most tasks never touch a paid model.
- **Proactive conductor** — every 15 minutes, AURA analyzes its own state (errors, pending tasks, git diff, test results), generates an improvement plan, and executes it. No prompting required.
- **Local-first** — embeddings, vector search, and most inference run on your machine. Nothing leaves your hardware unless you explicitly route to a cloud brain.

---

## 🧠 Why AURA exists — What Claude alone can't do

Claude is a powerful reasoning model. But the Claude chat interface is reactive: it waits for you. AURA makes it autonomous.

| Capability | Claude chat | AURA |
|-----------|-------------|------|
| 24/7 operation | No | Yes — LaunchAgent, always running |
| Scheduled tasks | No | Yes — APScheduler cron jobs |
| Local filesystem access | No | Yes — read/write any file on your Mac |
| Proactive self-improvement | No | Yes — conductor loop, every 15 min |
| Cost routing (free first) | No | Yes — 11 brains, $0 for most tasks |
| Persistent memory across sessions | No | Yes — local vector RAG + markdown files |
| Self-healing on failure | No | Yes — watchdog + auto-restart |
| Image generation | No | Yes — FLUX.1 via pollinations.ai |
| Voice messages | No | Yes — edge-tts responses |
| Social media pipeline | No | Yes — N8N integration |

Specifically, AURA enables:

**24/7 autonomous operation.** Runs while you sleep. Executes scheduled tasks on cron schedules. The conductor analyzes the codebase and queues improvement tasks every 15 minutes without any prompting.

**Local filesystem access.** Reads, writes, and edits any file under your `APPROVED_DIRECTORY`. Runs bash commands, manages git repos, executes tests. All via subprocess, no sandbox restrictions.

**Proactive conductor.** The most distinctive feature. Every 15 minutes, a 3-layer conductor runs:
1. Analysis layer — scans error logs, test results, git diff, pending tasks
2. Implementation layer — writes code fixes, new features, or refactors
3. Verification layer — syntax check, pytest validation, auto-commit

This loop has auto-improved AURA's own architecture multiple times without manual intervention.

**Scheduled routines.** Cron-style tasks managed by APScheduler. Auto-created by the conductor when it detects recurring patterns (via `ROUTINE:` signals in its output). Persistent across restarts. Full dashboard UI to manage them.

**RAG memory.** Local vector search over your memory files, mission documents, conductor logs, and notes. 768-dim embeddings via `nomic-embed-text` (Ollama, fully local). Context injected into the conductor before each autonomous task. Survives across sessions.

**Brain routing with cost optimization.** The router checks the task intent and picks the cheapest capable brain. Shell commands: zero tokens. General chat: Qwen (1000 free requests/day). Web search: Gemini (free). Code generation: local Ollama → Qwen → OpenCode cascade. Complex reasoning: Claude Haiku → Sonnet → Opus.

**Self-healing.** The watchdog pings the Telegram API every 2 minutes. After 3 consecutive failures: Telegram notification to you + `SIGTERM` to the bot process. The LaunchAgent's `KeepAlive = true` restarts it within 10 seconds.

**Image generation.** FLUX.1 via `pollinations.ai` — free, no API key. Triggered by natural language in any language.

**Voice TTS.** AURA can respond with voice messages via `edge-tts`.

**Social media pipeline.** Generates 5 images + captions and posts to Instagram/Twitter/LinkedIn via N8N webhooks.

---

## 🔒 Security Model

Security is layered from the outside in, with specific protections for autonomous operation.

**Five-layer defense:**

1. **Authentication** — Telegram user ID whitelist. Only configured users can interact.
2. **Directory isolation** — all file operations sandboxed to `APPROVED_DIRECTORY`. Path traversal blocked (`..`, absolute paths outside sandbox).
3. **Input validation** — blocks `;`, `&&`, `$()`, backticks, shell injection patterns, and access to secret files (`.env`, `.ssh`, `.pem`, `id_rsa`).
4. **Rate limiting** — per-user token bucket prevents abuse.
5. **Audit logging** — every action recorded in SQLite with user ID, timestamp, and content.

**Protected core files — autonomous loop denylist:**

The conductor has write access to the entire codebase, but nine files are in a `frozenset` it can never auto-stage or auto-commit:

```python
_PROTECTED_CORE_FILES: frozenset[str] = frozenset({
    "src/infra/proactive_loop.py",    # the loop itself
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

Any conductor attempt to stage these is dropped and logged as a warning. This prevents the autonomous loop from accidentally bricking its own engine.

**Selective git staging.** The conductor never runs `git add -A`. It stages only explicit files. After every commit, pytest runs. If tests fail, the commit is automatically reverted.

**Secret file filter.** Files matching `.env`, `secret`, `credential`, `token`, `password`, `private_key` patterns are silently skipped — even if the conductor tries to stage them.

**Watchdog protection.** Active ping loop (not just crash detection). 2-minute interval, 3-strike SIGTERM, LaunchAgent restart within 10 seconds.

**No credentials in code.** All secrets via environment variables, validated at startup. No tokens in source.

**Brain isolation.** Each brain (claude, codex, gemini, ollama, opencode) runs in a subprocess with its own auth context. A failure in one brain doesn't cascade to others.

---

## 🏗️ Architecture — The Matryoshka Stack

```
Layer 0: LaunchAgent (macOS, KeepAlive=true, 10s ThrottleInterval)
         ↓
Layer 1: Python bot process (src/main.py)
         ↓
Layer 1.5: Watchdog ping loop (2-min interval, 3-strike SIGTERM → LaunchAgent restart)
         ↓
Layer 2: Conductor / Proactive Loop (src/infra/proactive_loop.py) — 15-min autonomous task cycle
         ↓
Layer 2.5: _PROTECTED_CORE_FILES denylist (9 engine files, never auto-committed)
         ↓
Layer 3: Brain Router → AuraCortex (EMA scoring, bypass rules) → picks cheapest capable brain
         ↓
Layer 4: Routines Scheduler (APScheduler, cron jobs, SQLite-persisted)
         ↓
Layer 4.5: RAG System (nomic-embed-text via Ollama + SQLite vector store at ~/.aura/rag.db)
         ↓
Layer 5: FastAPI server (port 8080) + Cloudflare tunnel (auto-generated HTTPS URL)
         ↓
Layer 5.5: Dashboard (real-time SSE, Routines panel, Squad panel, RAG panel)
```

**Supporting systems running in parallel:**

- `AutoExecutor` — picks up pending tasks every 5 minutes, executes with the right brain
- `SelfEvaluator` — scans codebase every 30 minutes, auto-creates fix tasks when issues are found
- `EventBus` — async pub/sub connecting webhooks → agent → notifications
- `NotificationService` — rate-limited Telegram delivery for background task results

---

## 🔀 Brain Cascade — Cost-Optimized Routing

Eleven brains in cost-ascending order. The router picks the cheapest capable brain per request. The Cortex tracks historical success rates and auto-creates bypass rules for failing brain/intent pairs.

| Brain | Cost | Use case |
|-------|------|----------|
| `zero-token` | $0 | Bash, git, file ops — no LLM, instant |
| `api-zero` | $0 | Weather, crypto, QR codes via free public APIs |
| `ollama-rud` | $0 | Remote LAN Ollama server, code-focused, unlimited |
| `qwen-code` | $0 | Alibaba Qwen Code CLI, 1000 req/day free |
| `opencode` | $0 | OpenCode CLI, OpenRouter backend, free code gen |
| `gemini` | $0 | Google Gemini CLI, free tier, web-aware |
| `openrouter` | $0 | OpenRouter HTTP, free public model cascade |
| `cline` | $0 | Local Ollama, $0 if Ollama running locally |
| `codex` | $0 | OpenAI Codex via ChatGPT Plus |
| `haiku` | subscription | Claude Haiku via CLI, fast, reliable baseline |
| `sonnet` | subscription | Claude Sonnet via CLI, balanced complexity |
| `opus` | subscription | Claude Opus via CLI, deepest reasoning |

**Default cascade for code tasks:**
```
ollama-rud → qwen-code → opencode → gemini → openrouter → cline → haiku → sonnet → opus
```

**How the meta-router decides escalation:**
- Score < 5 → free tier (gemini / openrouter / cline)
- Score 5–14 → Haiku (Claude, moderate complexity)
- Score ≥ 15 → Sonnet (Claude, high complexity)

**AURA Cortex — self-learning routing layer:**

The Cortex sits above the router and learns from every interaction via EMA scoring (alpha = 0.15 — 15% new data, 85% history). It tracks success rate and latency per brain per intent type. If a brain fails 2+ times for a given intent, it creates a bypass rule and skips that brain for that intent. Everything persists to `~/.aura/cortex.json` and survives restarts.

```json
{
  "brain_scores": {
    "haiku:code": {"ema_success": 0.94, "avg_latency_s": 9.2, "score": 0.85},
    "ollama-rud:code": {"ema_success": 0.71, "avg_latency_s": 14.1, "score": 0.60}
  },
  "error_patterns": [
    {"intent": "search", "brain": "haiku", "count": 3, "bypass_active": true}
  ],
  "total_interactions": 847
}
```

---

## 🔍 RAG System

AURA has a local vector search system that indexes your memory files, mission documents, conductor logs, and notes. Context is injected into the conductor before each autonomous task so it operates with knowledge of your projects and past decisions.

- **Embeddings**: `nomic-embed-text` via Ollama, fully local, 768-dim vectors
- **Storage**: SQLite at `~/.aura/rag.db`, numpy cosine similarity
- **Indexing**: content-hash based, skips unchanged chunks — only re-embeds what changed
- **Sources**: `~/.aura/memory/` files, mission docs, conductor logs, markdown notes
- **Auto re-index**: every 5 minutes in the background
- **Context injection**: top-K chunks retrieved before each conductor task
- **API**: `GET /api/rag/search?q=query&top_k=5`, `GET /api/rag/status`, `POST /api/rag/index`

The RAG system means AURA's conductor decisions are informed by your project history, not just the current task. When it analyzes a bug, it knows what was tried before.

---

## ⏰ Routines System

Routines are cron-style scheduled tasks that run on a fixed schedule. They're created in two ways: manually via the dashboard, or auto-proposed by the conductor when it detects a recurring pattern.

The conductor emits `ROUTINE: <name> | <description> | <frequency>` signals in its output. The proactive loop parses these and creates routines automatically.

- **Storage**: SQLite, persists across restarts
- **Scheduler**: APScheduler with cron expression support
- **Brains**: any brain (codex, opencode, gemini, ollama) assignable per routine
- **API**: full CRUD at `/api/routines` (GET, POST, PUT, DELETE)
- **Dashboard panel**: list, toggle, run on-demand, view last result

---

## 📊 Dashboard

A single-page dashboard at `http://localhost:8080` with real-time SSE updates.

Panels:
- **Home** — KPI strip: bot status, RAM, disk, task counts, error rate (live)
- **Conductor** — current and recent autonomous task output
- **Routines** — list, toggle, run on-demand
- **Squad** — multi-agent task assignment
- **Tasks** — filter by All/Pending/Running/Done/Failed
- **Sessions** — Telegram conversation history
- **Brains** — status, latency, rate-limit state per brain
- **Cortex** — learned routing intelligence: EMA scores, bypass rules, total interactions
- **RAG** — index status, search interface
- **Terminal** — quick shell panel
- **Logs** — scrollable, filterable by level

Panel state persists to `localStorage`. Accessible externally via Cloudflare tunnel — auto-generated HTTPS URL, no configuration needed.

---

## 💰 Cost Savings

Running 50+ automation tasks per day through a single Claude API key adds up fast. AURA routes around it.

**Without AURA** (direct API calls for 50 daily tasks): ~$50–200/month depending on model mix.

**With AURA:**
- ~80% of tasks go to free brains (Ollama, Qwen, Gemini, OpenCode, zero-token)
- Claude MAX subscription covers the remaining ~20% that genuinely need it
- RAG eliminates redundant context loading (~$0.10 saved per session by not re-injecting project context)
- The conductor runs autonomously 24/7, generating improvements that would otherwise require manual Claude sessions

For typical usage: **effective additional cost above Claude MAX subscription ≈ $0.**

---

## 🤝 How Claude Collaborates on This Project

Claude (via `claude` CLI) is one of the brains — but AURA orchestrates it.

The codebase was built collaboratively: Claude writes code, AURA's conductor tests it and commits it, and the proactive loop identifies what to improve next. This creates a feedback loop:

1. Ricardo describes a feature in Telegram
2. AURA queues a task, routes it to the appropriate brain (usually Sonnet for architecture, Haiku for implementation)
3. Claude writes the code with full filesystem access
4. The conductor's verification layer runs syntax checks and pytest
5. Auto-commit if tests pass; auto-revert if they fail
6. Next conductor cycle picks up the commit, reviews it, and may spawn follow-up tasks

The conductor has auto-improved AURA's own architecture multiple times without manual prompting — the RAG system, the Cortex bypass rules, the watchdog ping loop, and the `_PROTECTED_CORE_FILES` denylist all emerged from conductor-initiated improvements, not from manual feature requests.

AURA can also ask Claude to review its own autonomous commits — a meta-loop where the orchestrator asks its own brain to evaluate recent decisions.

---

## 🚀 Installation

**Requirements:** Python 3.11+, `uv`, Claude CLI authenticated via Anthropic subscription (`claude` in PATH), Ollama with `nomic-embed-text`.

```bash
git clone https://github.com/royaluniondesign-sys/claude-code-telegram
cd claude-code-telegram

# Install dependencies
uv install

# Configure
cp .env.example .env
# Fill in at minimum:
# TELEGRAM_BOT_TOKEN=...   (from @BotFather)
# APPROVED_DIRECTORY=/Users/yourname

# Install Ollama and the embedding model
brew install ollama
ollama pull nomic-embed-text

# Install and start the LaunchAgent
cp src/infra/com.aura.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aura.bot.plist
```

**Minimum `.env`:**

```bash
TELEGRAM_BOT_TOKEN=your-token-from-botfather
TELEGRAM_BOT_USERNAME=your_bot_username
ALLOWED_USERS=your-telegram-user-id
APPROVED_DIRECTORY=/Users/yourname
```

**Enable dashboard and agentic mode:**

```bash
AGENTIC_MODE=true
ENABLE_API_SERVER=true
API_SERVER_PORT=8080
NOTIFICATION_CHAT_IDS=your-telegram-user-id
```

**Optional brains (all free when configured):**

```bash
GEMINI_ENABLED=true           # Gemini CLI (google auth, free tier)
OPENROUTER_API_KEY=sk-or-...  # OpenRouter free tier
RUD_OLLAMA_URL=http://...     # Remote Ollama server
```

**Dev commands:**

```bash
uv run make dev               # install all deps including dev tools
uv run make run               # run the bot
uv run make run-debug         # debug logging
uv run make test              # tests with coverage
uv run make lint              # black + isort + flake8 + mypy
uv run make format            # auto-format
```

---

## 🛠️ Tech Stack

| Component | Library / Tool |
|-----------|---------------|
| Language | Python 3.11–3.13 |
| Telegram interface | python-telegram-bot |
| API server | FastAPI |
| Task scheduler | APScheduler |
| Database | SQLite + aiosqlite |
| Embeddings | Ollama (nomic-embed-text) |
| Vector math | numpy |
| Logging | structlog (JSON prod, console dev) |
| Dependency management | uv |
| Claude interface | `claude` CLI via subprocess (no SDK) |

---

## 📂 Project Layout

```
src/
├── brains/          # Brain implementations + Cortex + BrainRouter
│   ├── cortex.py    # Self-learning EMA routing layer
│   ├── router.py    # Intent-based routing with cascade logic
│   ├── conductor.py # 3-layer conductor (analysis → impl → verify)
│   ├── claude_brain.py
│   ├── gemini_brain.py
│   ├── ollama_rud_brain.py
│   ├── qwen_brain.py
│   ├── openrouter_brain.py
│   └── image_brain.py
├── infra/           # Runtime infrastructure
│   ├── proactive_loop.py  # 15-min autonomous conductor cycle
│   ├── watchdog.py        # Ping loop + SIGTERM self-restart
│   ├── auto_executor.py   # Background task runner
│   └── com.aura.bot.plist # macOS LaunchAgent
├── rag/             # Local vector search
│   ├── embedder.py  # Async Ollama embedder + LRU cache
│   ├── indexer.py   # Incremental file indexer (content-hash)
│   ├── retriever.py # Cosine similarity search
│   └── store.py     # SQLite vector store (~/.aura/rag.db)
├── economy/         # Intent classification
│   ├── intent.py    # Regex-based intent detection
│   └── semantic_intent.py  # Embedding-based classification
├── scheduler/       # APScheduler cron jobs + routines store
├── bot/             # Telegram handlers, middleware, orchestrator
├── claude/          # Claude CLI facade, session management
├── api/             # FastAPI server, webhooks, dashboard routes
├── storage/         # SQLite repositories (sessions, audit, costs)
├── security/        # Auth, input validation, rate limiting, audit
├── events/          # Async pub/sub EventBus
├── notifications/   # Rate-limited Telegram delivery
└── context/         # Session context, fact extraction
dashboard/           # Single-file dashboard UI (SSE live updates)
scripts/             # rud_server_setup.sh, export_chat_history.py
config/              # projects.example.yaml
```

---

## 📈 Project Status

- **Tests**: 491 passing, 25% coverage (growing — conductor adds tests each cycle)
- **Version**: 0.10.0
- **Branch**: `feat/dashboard-mission-control` (active development sprint)
- **Stability**: Beta — core routing and autonomous loop are production-stable; dashboard and RAG are actively evolving

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <strong>AURA — your hardware, your models, your rules.</strong>
</p>
