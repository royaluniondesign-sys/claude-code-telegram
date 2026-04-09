<p align="center">
  <img src="https://img.shields.io/badge/AURA-Personal_AI_Agent-7c5cff?style=for-the-badge&labelColor=0c0c14" alt="AURA">
</p>

<h1 align="center">AURA</h1>
<p align="center"><strong>Personal AI Agent System</strong></p>
<p align="center">3 Brains · Zero API Keys · One Soul</p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
  <a href="https://hub.docker.com"><img src="https://img.shields.io/badge/docker-ready-2496ED.svg" alt="Docker"></a>
</p>

---

AURA is a **multi-brain personal AI agent** that runs on Telegram. It orchestrates Claude, Codex, and Gemini — using your existing subscriptions via CLI auth. **No API keys. No extra costs.**

## Why AURA?

You already pay for Claude Pro ($20/mo), ChatGPT Plus ($20/mo), and Google gives you 1000 free requests/day. AURA makes all three work together as one agent — from your phone.

| Feature | AURA | Other Bots |
|---------|------|------------|
| Multi-brain (3 LLMs) | ✅ Claude + Codex + Gemini | ❌ Single model |
| Zero API keys | ✅ CLI subscription auth | ❌ API keys required |
| Zero-token commands | ✅ 28+ free commands | ❌ Every action costs tokens |
| Self-healing | ✅ Auto-restart + alerts | ❌ Manual monitoring |
| Business automation | ✅ Email, standup, reports | ❌ Chat only |
| Cross-device context | ✅ Terminal ↔ Telegram | ❌ No handoff |
| Voice & Vision | ✅ Whisper + Gemini Vision | ❌ Text only |
| Docker-ready | ✅ One command deploy | ❌ Complex setup |

## Quick Start

### Option A: One-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/royaluniondesign-sys/aura/main/install.sh | bash
```

### Option B: Docker

```bash
git clone https://github.com/royaluniondesign-sys/aura.git
cd aura
cp .env.example .env
# Edit .env with your Telegram token and user ID
docker compose up -d
```

### Option C: Manual

```bash
git clone https://github.com/royaluniondesign-sys/aura.git
cd aura
uv tool install --editable .
cp .env.example .env
# Edit .env
claude-telegram-bot
```

## Configuration

**Minimum required** (`.env`):

```bash
TELEGRAM_BOT_TOKEN=your-token-from-botfather
ALLOWED_USERS=your-telegram-user-id
APPROVED_DIRECTORY=/Users/yourname
```

**Optional features:**

```bash
AGENTIC_MODE=true              # Natural language mode (default)
ENABLE_SCHEDULER=true          # Business workflow automation
ENABLE_API_SERVER=true         # Webhook server
NOTIFICATION_CHAT_IDS=123      # Proactive notifications
DASHBOARD_PORT=3000            # Web dashboard port
VOICE_PROVIDER=whisper_local   # Voice transcription
```

> Full reference: [`.env.example`](.env.example)

## The Three Brains

| Brain | Subscription | Auth Command | Best For |
|-------|-------------|--------------|----------|
| 🟠 **Claude** | Pro $20 / Max $100 | `claude auth login` | Code, analysis, tools |
| 🟢 **Codex** | ChatGPT Plus $20 | `codex login` | Fast code generation |
| 🔵 **Gemini** | Free (Google) | `gemini` → browser | Search, translation, vision |
| 🟣 **Perplexity** | Pro $20 (optional) | API key | Real-time web search |

Switch brains from Telegram:

```
/brain codex     → Switch to Codex
/brain gemini    → Switch to Gemini
/brain claude    → Back to Claude
/brains          → See all status
```

Smart routing picks the best brain automatically:

```
bash/git/files     → Zero-token (no LLM)
code/refactor      → Claude or Codex
web search         → Gemini (free)
deep analysis      → Claude Opus
translation        → Gemini (fast + free)
```

## Zero-Token Commands

These execute **instantly without consuming any AI tokens**:

```
!ls, !pwd, !git status    → Shell passthrough
/ls /git /health          → Built-in commands
/terminal                 → Web terminal (clsh)
/costs                    → Token economy stats
/brain                    → Switch brain
/docker ps                → Container status
```

## Business Automation

AURA runs scheduled workflows while you sleep:

| Workflow | Schedule | What it does |
|----------|----------|-------------|
| Daily Standup | 8am Mon-Fri | Git activity + pending items + calendar |
| Email Triage | 8am daily | Classify inbox by priority |
| Client Follow-up | 5pm Friday | Unanswered emails > 48h |
| Weekly Report | 8pm Sunday | Full week summary + next priorities |

## Dashboard

Web dashboard at `localhost:3000` with:

- Real-time brain status and rate limits
- Service health monitoring
- Token economy tracking
- Workflow status
- Context bridge state
- Live logs

## Architecture

```
Telegram (@your_bot)
       │
   AURA Core (Python)
   ├── Brain Router ─── 🟠 Claude SDK
   │                ├── 🟢 Codex CLI
   │                ├── 🔵 Gemini CLI
   │                └── 🟣 Perplexity CLI
   ├── Scheduler (APScheduler)
   ├── Event Bus (async pub/sub)
   ├── Watchdog (self-healing)
   ├── Dashboard (FastAPI + HTMx)
   └── Storage (SQLite)
```

## Self-Healing

AURA monitors itself every 5 minutes:

- Service crashes → auto-restart
- 3 consecutive failures → Telegram alert to owner
- Rate limit hit → automatic brain fallback
- Memory corruption → auto-repair

## Development

```bash
make dev           # Install dependencies
make test          # Run tests
make lint          # Code quality
make run-debug     # Debug mode
```

## Docker

```bash
docker compose up -d          # Start
docker compose logs -f aura   # Logs
docker compose down           # Stop
```

The container mounts your host CLI auth configs (read-only) so you don't need to re-authenticate inside Docker.

## Security

- **Access Control** — Whitelist-based user authentication
- **Directory Isolation** — Sandboxed to approved directories
- **Rate Limiting** — Per-user token bucket
- **CLI Auth** — No API keys stored, no secrets in code
- **Audit Logging** — Full action history

## Roadmap

- [x] Phase 1: Core bot + Claude SDK
- [x] Phase 2: Multi-brain (Claude + Codex + Gemini)
- [x] Phase 3: Email & Calendar (partial — needs OAuth)
- [x] Phase 4: Self-healing infrastructure
- [x] Phase 5: Smart routing + token economy
- [x] Phase 6: Business workflows
- [x] Phase 7: Web terminal (clsh)
- [x] Phase 8: Voice & vision
- [x] Phase 9: Dashboard + OSS packaging
- [ ] Phase 10: Multi-machine (SSH fleet, shared memory)
- [ ] Phase 11: Monetization (AURA Cloud, Teams)

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built by [RUD Agency](https://rud-web.vercel.app) with [Claude](https://claude.ai) + [Telegram](https://telegram.org).

---

<p align="center">
  <strong>AURA — Three brains. Zero API keys. One soul.</strong>
</p>
