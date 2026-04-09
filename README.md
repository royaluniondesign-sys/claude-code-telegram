<p align="center">
  <img src="https://img.shields.io/badge/AURA-Mission_Control-d97757?style=for-the-badge&labelColor=0e0d0c&color=d97757" alt="AURA">
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
Autonomous Universal Remote Agent
```

**Remote autonomous agent via Telegram.** Multi-brain orchestration (Claude + Gemini), zero-token shell passthrough, self-healing task system, mission control dashboard, and web terminal. Runs on your machine, controlled from your phone.

---

## What it actually does

- **Talk to Claude from Telegram** — full Claude SDK streaming, session persistence across conversations
- **Multi-brain routing** — Claude handles code/analysis, Gemini CLI handles free search/translation
- **28+ zero-token commands** — shell passthrough, git ops, health checks without burning AI tokens
- **Autonomous task system** — queue background tasks, let AURA self-execute while you sleep
- **Self-evaluation** — AURA scans codebase every 30min, auto-creates fix tasks for issues it finds
- **Mission control dashboard** — FastAPI server at `:8080` with live KPIs, execution panel, log tail
- **Web terminal** — Termora PTY server at `:4030` with ngrok tunnel, one-click access from phone
- **Webhook server** — GitHub push hooks trigger Claude automatically on PRs and commits
- **Voice messages** — Transcribe via Mistral or OpenAI, then route to Claude
- **Multi-project topics** — Separate Telegram threads per project, YAML-configured

---

## Architecture

```
Telegram (@your_bot)
       │
   MessageOrchestrator (Python, python-telegram-bot)
   ├── Zero-Token Handler ─── instant commands, no LLM, no cost
   │   ├── /terminal      → Termora auth link
   │   ├── /status        → system health
   │   ├── /task          → queue background task
   │   └── !shell cmds    → bash passthrough
   │
   ├── Agentic Mode ─── streaming Claude SDK
   │   ├── ClaudeIntegration (facade)
   │   ├── ClaudeSDKManager (async streaming)
   │   ├── Session manager (per-user+dir, SQLite-persisted)
   │   └── ToolMonitor (validates tool calls)
   │
   ├── Brain Router ─── smart routing
   │   ├── 🟠 Claude SDK (subscription auth)
   │   ├── 🔵 Gemini CLI (google auth, free tier)
   │   └── Codex CLI (optional, ChatGPT Plus)
   │
   ├── Task System ─── autonomous execution
   │   ├── TaskManager (~/.aura/tasks.json)
   │   ├── AutoExecutor (runs pending every 5min)
   │   └── SelfEvaluator (scans codebase every 30min)
   │
   ├── API Server (FastAPI :8080)
   │   ├── Dashboard /           → Mission control
   │   ├── Webhook /webhooks/    → GitHub, generic
   │   └── API /api/*            → Status, tasks, logs
   │
   ├── Event Bus (async pub/sub)
   │   ├── WebhookEvent → AgentHandler → ClaudeIntegration
   │   └── AgentResponseEvent → NotificationService
   │
   ├── Scheduler (APScheduler + SQLite)
   │   └── Cron jobs: standup, triage, reports
   │
   ├── Termora (PTY :4030)
   │   ├── WebSocket streaming
   │   ├── ngrok tunnel
   │   └── LaunchAgent (auto-restart)
   │
   └── Storage (SQLite, aiosqlite)
       ├── users, sessions, messages
       ├── tool_usage, audit_log, cost_tracking
       └── project_threads, webhook_events
```

---

## Security

5-layer defense:

1. **Auth** — whitelist by Telegram user ID
2. **Directory isolation** — sandboxed to `APPROVED_DIRECTORY`, path traversal blocked
3. **Input validation** — blocks `..`, `;`, `&&`, `$()`, shell injection patterns
4. **Rate limiting** — per-user token bucket
5. **Audit logging** — full action history in SQLite

Webhook auth: GitHub HMAC-SHA256 signature verification. Generic endpoints use Bearer token. Atomic deduplication on `webhook_events` table.

---

## Setup

**Requirements:** Python 3.11+, Poetry, Claude CLI authenticated (`claude` subscription)

```bash
git clone https://github.com/royaluniondesign-sys/claude-code-telegram.git
cd claude-code-telegram
make dev
cp .env.example .env
# Edit .env
make run
```

**Minimum `.env`:**

```bash
TELEGRAM_BOT_TOKEN=your-token-from-botfather
TELEGRAM_BOT_USERNAME=your_bot_username
ALLOWED_USERS=your-telegram-user-id
APPROVED_DIRECTORY=/Users/yourname
```

**Agentic + dashboard:**

```bash
AGENTIC_MODE=true
ENABLE_API_SERVER=true
API_SERVER_PORT=8080
NOTIFICATION_CHAT_IDS=your-telegram-user-id
```

**Optional brains:**

```bash
GEMINI_ENABLED=true            # Gemini CLI (google auth, free)
CODEX_ENABLED=true             # Codex CLI (ChatGPT Plus)
```

**Termora web terminal:**

```bash
# Install and start Termora
cd ~/Projects/termora && npm run dev

# Or via LaunchAgent (auto-start at login)
launchctl load ~/Library/LaunchAgents/com.termora.agent.plist
```

---

## Zero-Token Commands

Execute instantly — no LLM, no cost:

| Command | What it does |
|---------|-------------|
| `/terminal` | Web terminal link (Termora, ngrok-tunneled) |
| `/status` | System health: RAM, disk, bot uptime |
| `/task <brain> <prompt>` | Queue background task for any brain |
| `/tasks` | List all tasks (pending/running/done/failed) |
| `/verbose 0\|1\|2` | Output verbosity per session |
| `/new` | Fresh Claude session |
| `/repo` | Current working directory |
| `!ls`, `!pwd`, `!git status` | Shell passthrough |

---

## Task System

AURA queues and executes background tasks autonomously:

```bash
# From Telegram:
/task claude "refactor the auth module and run tests"
/task gemini "summarize this week's commits"

# View status:
/tasks
```

Tasks persist in `~/.aura/tasks.json`. AutoExecutor picks up pending tasks every 5 minutes. SelfEvaluator scans the codebase every 30 minutes and creates fix tasks automatically.

Telegram notifications on:
- ✅ Task complete (with output snippet)
- ❌ Task failed after 3 attempts
- 🔍 New tasks auto-detected by SelfEvaluator

---

## Dashboard

Mission control at `localhost:8080`:

- **KPI strip** — BOT status, RAM, disk, task counts, error rate (live)
- **Execution panel** — real-time output of running task, or last completed
- **Brain grid** — status of each brain (Claude/Gemini/Codex) with latency
- **Tasks** — filter tabs (All/Pending/Running/Done/Failed), run/view/delete
- **Logs** — full scrollable log tail with level filter (DEBUG/INFO/WARNING/ERROR)
- **Execute** — shell terminal with quick actions
- **Crons** — scheduled job status

---

## Webhooks

GitHub pushes trigger Claude automatically:

```bash
# In your repo webhook settings:
Payload URL: https://your-server.com/webhooks/github
Secret: $GITHUB_WEBHOOK_SECRET
Events: push, pull_request
```

```bash
ENABLE_API_SERVER=true
GITHUB_WEBHOOK_SECRET=your-secret
WEBHOOK_API_SECRET=your-bearer-token
```

---

## Multi-Project Topics

Route each project to its own Telegram thread:

```bash
ENABLE_PROJECT_THREADS=true
PROJECT_THREADS_MODE=private        # or group
PROJECTS_CONFIG_PATH=config/projects.yaml
```

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

## Dev

```bash
make dev              # Install all deps (includes dev)
make run              # Run the bot
make run-debug        # Debug logging
make test             # Tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format

# Single test
poetry run pytest tests/unit/test_config.py -k test_name -v
```

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <strong>AURA — Claude on your phone. Always on. Always autonomous.</strong>
</p>
