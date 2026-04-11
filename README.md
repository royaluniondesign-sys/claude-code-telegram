<p align="center">
  <img src="https://img.shields.io/badge/AURA-v0.10.0-d97757?style=for-the-badge&labelColor=0e0d0c&color=d97757" alt="AURA">
  <img src="https://img.shields.io/badge/Claude_SDK-Subscription_Auth-7c5cff?style=for-the-badge&labelColor=0e0d0c" alt="Claude SDK">
  <img src="https://img.shields.io/badge/Python-3.11+-3776ab?style=for-the-badge&labelColor=0e0d0c" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge&labelColor=0e0d0c" alt="MIT">
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

**Personal AI agent that runs on your machine, controlled from your phone.**

AURA connects Telegram to a multi-brain AI system on your Mac. It routes requests to the cheapest brain that can handle them — shell commands cost zero tokens, simple chat goes to a fast model, deep analysis escalates to Claude Opus. The routing layer learns from every interaction and gets smarter over time.

No cloud intermediary. No subscription SaaS. Your hardware, your credentials, your data.

---

## Why this exists

Most "AI assistant" setups are one model behind a chat interface. You pick the model, you pay per token, you get no routing intelligence.

AURA takes a different approach: treat AI models as a cascade of brains with different costs, speeds, and capabilities. A bash command doesn't need Opus. A translation doesn't need a code model. An architectural decision probably shouldn't go to the free tier. Route intelligently, learn from failures, escalate only when necessary.

The result is an agent that's fast for simple things, powerful for hard things, and doesn't burn your token budget on trivia.

---

## Multi-Brain Cascade

Eight brains in cost-ascending order:

| Brain | What it is | Cost |
|-------|-----------|------|
| `zero-token` | Bash/git/file ops executed directly | $0 — no LLM |
| `api-zero` | Weather, crypto, currency via free APIs | $0 — no LLM |
| `ollama-rud` | Ollama on a remote Ubuntu server | $0 — open source |
| `haiku` | Claude 3.5 Haiku via subscription CLI | Subscription (fast workhorse) |
| `gemini` | Google Gemini CLI (web-aware) | Free tier |
| `openrouter` | 50+ models via OpenRouter free cascade | Free tier / configurable |
| `sonnet` | Claude 3.5 Sonnet via subscription CLI | Subscription (balanced) |
| `opus` | Claude 3 Opus via subscription CLI | Subscription (deep reasoning) |

The router picks the cheapest brain that can handle the request. If that brain fails or is rate-limited, it walks up the cascade automatically.

### How routing decisions are made

1. **Zero-token detection** — regex patterns catch shell commands, git ops, file paths. Executed directly via subprocess. No LLM involved.
2. **Semantic intent classification** — 12 intent types (`bash`, `files`, `git`, `search`, `translate`, `code`, `chat`, `email`, `calendar`, `deep`, `image`, plus API-zero). Uses local fastembed embeddings (first run downloads ~50MB, subsequent calls ~5ms). Falls back to regex if embedding model fails.
3. **Meta-router complexity score** — scores the request on multiple factors. Score < 5 stays on free tier. Score 5–14 goes to Haiku. Score ≥ 15 escalates to Sonnet.
4. **Pre-flight rate-limit check** — before sending, checks if the selected brain is currently rate-limited. If yes, walks the cascade to find an available brain.

---

## AURA Cortex — Self-Learning Routing

The Cortex sits above the router and learns from every interaction.

**What it tracks:**
- Success rate and latency per brain per intent type
- Failure patterns: which brain fails for which kind of request

**What it does with that data:**
- Maintains an EMA score (15% new data, 85% history) for each brain/intent pair
- If brain X fails 2+ times for intent Y, creates a bypass rule: skip brain X for that intent
- Persists everything to `~/.aura/cortex.json` — survives restarts, accumulates across sessions

**Session context enrichment:**
- Tracks the last 10 intents and topic keywords from recent prompts
- Enriches new prompts with that context automatically

The Cortex never crashes the bot — all errors are caught and logged. If the Cortex fails, routing falls through to the base router.

```json
// ~/.aura/cortex.json (excerpt)
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

## Architecture

```
Telegram
       |
   Security Middleware (input validation, path traversal blocking)
   Auth Middleware (Telegram user whitelist)
   Rate Limit Middleware (per-user token bucket)
       |
   MessageOrchestrator
       |
   AuraCortex (learned routing layer)
       |
   BrainRouter (intent classifier + meta-router + cascade)
       |
   ┌───────────┬──────────┬──────────┬──────────┬──────────┐
   zero-token  api-zero  ollama-rud  haiku    gemini/openrouter/sonnet/opus
       |
   SQLite Storage (sessions, messages, audit_log, cost_tracking)
       |
   Telegram Response (streaming)
```

**Supporting systems running in parallel:**

- `FastAPI` server at `:8080` — dashboard, webhooks, REST API
- `APScheduler` — cron jobs (standup, triage, reports)
- `AutoExecutor` — picks up pending tasks every 5 minutes
- `SelfEvaluator` — scans codebase every 30 minutes, auto-creates fix tasks
- `EventBus` — async pub/sub connecting webhooks → agent → notifications

---

## Features

### Zero-Token Commands

Execute instantly. No LLM, no cost.

```
!ls -la ~/Projects          # shell passthrough
!git status                 # git ops
/status                     # system health: RAM, disk, uptime
/task haiku "summarize commits from this week"
/tasks                      # list all queued/running/done tasks
/verbose 0|1|2              # output verbosity per session
/new                        # fresh Claude session
```

### Image Generation

FLUX.1 via pollinations.ai — free, no API key, triggered by natural language in Spanish or English:

```
"genera una imagen de un paisaje futurista en la noche"
"make a poster for a jazz festival, art deco style"
```

Returns 1024x1024 JPEG directly in Telegram.

### Voice Messages

Send a voice note to Telegram. AURA transcribes it via Mistral or OpenAI Whisper, then routes the transcribed text through the normal brain cascade.

### Autonomous Task System

```bash
# Queue a background task from Telegram:
/task claude "refactor the auth module and run tests"
/task gemini "research the top 5 Rust async runtimes"

# Check status:
/tasks
```

Tasks persist in `~/.aura/tasks.json`. `AutoExecutor` runs pending tasks every 5 minutes. `SelfEvaluator` scans the codebase every 30 minutes and auto-creates fix tasks when it finds issues.

Telegram notifications on task completion, failure (after 3 attempts), and auto-detected tasks.

### Dashboard (port 8080)

Live activity feed via SSE. Panels:

- **KPI strip** — bot status, RAM, disk, task counts, error rate (live)
- **Brain monitor** — status, latency, and rate-limit state per brain
- **Task panel** — filter by All/Pending/Running/Done/Failed, run/view/delete
- **Cortex panel** — learned routing intelligence: scores, bypass rules, interaction count
- **RUD panel** — remote Ubuntu server status (Ollama, N8N, Grafana)
- **Log tail** — scrollable, filterable by level
- **Shell panel** — quick action terminal

### GitHub Webhooks

```bash
# Configure your repo:
Payload URL: https://your-server.com/webhooks/github
Secret: $GITHUB_WEBHOOK_SECRET
Events: push, pull_request
```

GitHub pushes trigger Claude automatically. HMAC-SHA256 signature verification. Atomic deduplication on `webhook_events` table prevents double-processing.

### Multi-Project Topics

Route each project to its own Telegram thread:

```yaml
# config/projects.yaml
projects:
  - name: my-app
    path: /Users/me/my-app
    description: "Main product"
  - name: scripts
    path: /Users/me/scripts
    description: "Automation scripts"
```

---

## Remote Server (RUD) Integration

AURA can offload inference to a remote Ubuntu server running Ollama. This gives you free GPU inference for code and general tasks without touching your local machine's resources.

Setup is handled by `scripts/rud_server_setup.sh` — run it on the remote server. It installs Ollama, configures ngrok for tunnel access, and sets up N8N and Grafana as optional extras.

```bash
# On the remote server:
bash rud_server_setup.sh
```

Configure the connection in `.env`:
```bash
RUD_OLLAMA_URL=http://<your-rud-server>:11434
```

AURA falls back gracefully when the RUD server is offline — the router detects unreachable hosts before sending requests.

---

## Security

Five-layer defense:

1. **Auth** — Telegram user ID whitelist. Only configured users can interact.
2. **Directory isolation** — sandboxed to `APPROVED_DIRECTORY`. Path traversal blocked (`..`, absolute paths outside sandbox).
3. **Input validation** — blocks `;`, `&&`, `$()`, backticks, and other shell injection patterns. Blocks access to `.env`, `.ssh`, `.pem`, and secret files.
4. **Rate limiting** — per-user token bucket prevents abuse.
5. **Audit logging** — every action recorded in SQLite with user, timestamp, and content.

Webhook authentication: GitHub HMAC-SHA256 signature verification. Generic endpoints use Bearer tokens.

`ToolMonitor` validates Claude's tool calls against an allowlist, file path boundaries, and dangerous bash patterns before execution.

No API keys in source code. All secrets via environment variables. Required secrets validated at startup.

---

## Quick Start

**Requirements:** Python 3.11+, Poetry, Claude CLI authenticated via Anthropic subscription (`claude` in PATH)

```bash
git clone https://github.com/royaluniondesign-sys/claude-code-telegram.git
cd claude-code-telegram
cp .env.example .env   # fill in the required values
poetry install
make run
```

**Minimum `.env`:**

```bash
TELEGRAM_BOT_TOKEN=your-token-from-botfather
TELEGRAM_BOT_USERNAME=your_bot_username
ALLOWED_USERS=your-telegram-user-id
APPROVED_DIRECTORY=/Users/yourname
```

**Enable the dashboard and agentic mode:**

```bash
AGENTIC_MODE=true
ENABLE_API_SERVER=true
API_SERVER_PORT=8080
NOTIFICATION_CHAT_IDS=your-telegram-user-id
```

**Optional brains (all free when configured):**

```bash
GEMINI_ENABLED=true           # Gemini CLI (google auth, free tier)
OPENROUTER_API_KEY=sk-or-...  # OpenRouter (free tier available)
RUD_OLLAMA_URL=http://...     # Remote Ollama server
```

**Voice transcription:**

```bash
ENABLE_VOICE_MESSAGES=true
VOICE_PROVIDER=mistral        # or openai
MISTRAL_API_KEY=...
```

**Dev commands:**

```bash
make dev              # install all deps including dev tools
make run              # run the bot
make run-debug        # debug logging
make test             # tests with coverage
make lint             # black + isort + flake8 + mypy
make format           # auto-format
```

---

## vs. Alternatives

| | AURA | Aider | Open Interpreter | Claude.ai app |
|---|---|---|---|---|
| Runs on your hardware | yes | yes | yes | no |
| Controlled via Telegram | yes | no | no | no |
| Multi-brain cascade | yes | no | no | no |
| Self-learning routing | yes | no | no | no |
| Zero-token shell ops | yes | partial | yes | no |
| Local file access | yes | yes | yes | no |
| Autonomous self-healing | yes | no | no | no |
| Image generation | yes (FLUX.1) | no | no | no |
| Voice input | yes | no | no | yes |
| No API key required | yes (subscription) | no | no | subscription |

**Aider** is excellent for code-focused terminal workflows. It has no routing intelligence and no mobile interface.

**Open Interpreter** is the closest conceptually — local execution, natural language interface. AURA adds the multi-brain cascade, self-learning Cortex, Telegram interface, and autonomous task system.

**Claude.ai app** has no local file access, no multi-brain routing, and no way to run shell commands. It's a chat interface, not an agent.

**Custom Telegram bots** are usually single-model with no routing, no learning, and no self-healing. Building that from scratch is the baseline — AURA is what it becomes after significant iteration.

AURA's core differentiator: it runs on your hardware, routes to the right brain for each task, learns from failures, and heals itself. That combination doesn't exist elsewhere as a complete system.

---

## Project Layout

```
src/
├── brains/          # Brain implementations + Cortex + router
│   ├── cortex.py    # Self-learning meta-orchestration layer
│   ├── router.py    # Intent-based routing with cascade
│   ├── claude_brain.py
│   ├── gemini_brain.py
│   ├── ollama_rud_brain.py
│   ├── openrouter_brain.py
│   └── image_brain.py
├── economy/         # Intent classification (regex + semantic)
│   ├── intent.py
│   └── semantic_intent.py
├── bot/             # Telegram handlers, middleware, orchestrator
├── claude/          # Claude SDK facade, session management
├── api/             # FastAPI server, webhooks
├── storage/         # SQLite repositories
├── security/        # Auth, input validation, rate limiting, audit
├── events/          # Async pub/sub event bus
├── scheduler/       # APScheduler cron jobs
├── notifications/   # Rate-limited Telegram delivery
└── context/         # Session context, fact extraction
dashboard/           # Single-file dashboard UI (SSE live updates)
scripts/             # rud_server_setup.sh, export_chat_history.py
config/              # projects.example.yaml
```

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <strong>AURA — your hardware, your models, your rules.</strong>
</p>
