# Hermes ↔ AURA Agent Mesh — Design Spec
_2026-04-29_

## Vision

Two autonomous agents running on Ricardo's Mac as always-on daemons, each with full domain ownership, communicating bidirectionally in real-time. Ricardo talks to either one; each can delegate to the other, work in parallel on the same project, and report results back.

Each agent has **complete visibility** into the other's state — skills, crons, memory, tasks, model, soul. No black boxes between them.

**Parallel project execution:** Ricardo assigns a project to either agent → the coordinator splits work → both execute independently → results merge into a shared project folder → Ricardo gets one consolidated update.

```
Ricardo
  ├── Telegram @aurajbot     → AURA  (Python/FastAPI :8080, Claude sub)
  └── Telegram @rudserverbot → Hermes (OpenClaw :18789, Ollama free)
                                  ↕ HTTP agent mesh
                           ~/.aura/ (shared memory + drafts)
```

---

## Agent Identities

### AURA
- **Runtime:** Python + python-telegram-bot + FastAPI
- **Brain:** Claude Max subscription (no API key, CLI auth)
- **Owns:** Social media pipeline, FLUX.1-dev images, blog publisher, dashboard (:4030), brain router cascade, memory system
- **Telegram:** existing bot

### Hermes
- **Runtime:** OpenClaw daemon (Node.js, port 18789)
- **Brain:** Ollama qwen2.5:14b (primary, local $0) → deepseek-r1:14b (reasoning) → NVIDIA Llama 3.3 70B (heavy, free nvapi key) → Gemini CLI (web search) → AURA proxy (Claude fallback, no extra charge)
- **Owns:** ClaHub skills ecosystem (5,211+ skills), multi-channel (WhatsApp/Discord/Signal), heartbeat scheduler, SOUL.md identity, OpenClaw Control UI (:18789)
- **Telegram:** @rudserverbot

---

## Agent Mesh Protocol

### Hermes → AURA
New FastAPI endpoint on AURA:

```
POST /api/agent-query
{
  "from": "hermes",
  "task": "string",           // natural language task
  "context": {},              // optional extra data
  "prefer_brain": "claude"    // haiku | sonnet | gemini | auto
}
→ { "ok": true, "result": "string", "brain_used": "claude-sonnet" }
```

AURA executes the task using its brain router, returns result. Hermes relays to Ricardo if needed.

### AURA → Hermes
AURA calls OpenClaw's native conversation injection API:

```
POST http://localhost:18789/api/conversations
{
  "message": "task description",
  "agentId": "hermes"
}
→ OpenClaw streams result back to AURA
```

AURA waits for response (async with timeout 60s), then delivers to Ricardo.

### Shared Memory (filesystem)
Both agents read and write to `~/.aura/memory/`:

| File | Owner (primary writer) | What it contains |
|---|---|---|
| `MEMORY.md` | AURA | Main index: owner, projects, preferences |
| `session-plan.md` | AURA | Current plan, next steps |
| `hermes.md` | Hermes | Hermes state, active skills, heartbeat log |
| `aura-for-hermes.md` | AURA | Context AURA leaves for Hermes between sessions |
| `mesh-log.md` | Both | Inter-agent delegation log (task, result, timestamp) |

---

## Hermes Configuration

### SOUL.md (identity)
```markdown
---
name: Hermes
version: 1.0
---

You are Hermes, Ricardo's second autonomous agent. You work alongside AURA.

## Your role
- Handle research, coding tasks, git, browser automation, multi-channel comms
- Delegate social media and Claude-heavy reasoning to AURA via HTTP
- Run proactively via heartbeat: check session-plan.md and advance tasks
- Never use Anthropic API directly — use AURA proxy for Claude reasoning

## AURA bridge
AURA runs at http://localhost:8080. To delegate:
  POST /api/agent-query {"task": "...", "prefer_brain": "claude"}

## Memory
Read ~/.aura/memory/MEMORY.md for full context about Ricardo and active projects.
Write discoveries to ~/.aura/memory/hermes.md.

## Rules
- Same language as Ricardo (Spanish/English)
- Concise — Telegram context
- When you delegate to AURA, tell Ricardo you're doing it and report the result
```

### openclaw.json (core config)
```json5
{
  env: {
    OLLAMA_API_KEY: "ollama-local",
    TELEGRAM_BOT_TOKEN: "<new-token-from-botfather>"
  },
  channels: {
    telegram: {
      botToken: "$TELEGRAM_BOT_TOKEN",
      dmPolicy: "allowlist",
      allowlist: ["854546789"]   // Ricardo's Telegram ID
    }
  },
  agents: {
    defaults: {
      model: {
        primary:  "ollama/qwen2.5:14b",
        fallback: "nvidia/meta/llama-3.3-70b-instruct"
      },
      skills: ["aura-bridge", "git-assistant", "web-research", "code-review"]
    }
  },
  models: {
    providers: {
      ollama: {
        baseUrl: "http://localhost:11434",
        timeoutSeconds: 120
      },
      nvidia: {
        baseUrl: "https://integrate.api.nvidia.com/v1",
        apiKey: "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"
      }
    }
  },
  heartbeat: {
    enabled: true,
    intervalMinutes: 60,
    task: "Read ~/.aura/memory/session-plan.md. If there are pending tasks you can advance, do one. Write result to ~/.aura/memory/hermes.md."
  }
}
```

### aura-bridge skill (SKILL.md)
```markdown
---
name: aura-bridge
description: Delegate tasks to AURA agent (Claude reasoning, social media, image generation)
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝"}}
---

# AURA Bridge

## When to use
- User asks to "tell AURA", "ask AURA", "have AURA do"
- Task requires Claude-level reasoning
- Task involves social media publishing or image generation
- Task involves blog publishing

## Workflow
1. Extract the task from the user message
2. POST http://localhost:8080/api/agent-query with {"from":"hermes","task":"<task>"}
3. Wait for JSON response (timeout 60s)
4. Report result to user: "AURA says: <result>"
5. Log to ~/.aura/memory/mesh-log.md: timestamp, task, result summary
```

---

## Full Mutual Transparency

### AURA sees all of Hermes
New endpoint on OpenClaw (native): `GET :18789/api/full-status`
Returns: active model, installed skills list, cron jobs + schedule, SOUL.md content, heartbeat log (last 10), tasks in queue, uptime.

AURA dashboard "Hermes" panel shows all of this — not just a health dot.

### Hermes sees all of AURA
New AURA endpoint: `GET :8080/api/agent-status`
Returns: brain router state (active brain, rate limits), social queue (pending posts), last 5 posts in history, memory files list + last modified, social roadmap backlog, dashboard URL, Termora URL.

Hermes skill `aura-context` calls this on demand or on heartbeat.

---

## Shared Project Space

```
~/.aura/projects/<project-slug>/
  ├── plan.md            — coordinator writes task split (who does what)
  ├── aura-progress.md   — AURA writes updates here
  ├── hermes-progress.md — Hermes writes updates here
  └── result.md          — last-to-finish agent consolidates + notifies Ricardo
```

### Project protocol
```
Ricardo → either agent: "lanza proyecto X con Hermes/AURA"

Coordinator (whoever received the message):
  1. Creates ~/.aura/projects/<slug>/plan.md with task split
  2. Executes own tasks
  3. POST to other agent: {"task": "...", "project_id": "<slug>", "write_to": "hermes-progress.md"}
  4. Both run in parallel, each writing progress to their file
  5. Coordinator polls for other agent's completion (checks hermes-progress.md / aura-progress.md)
  6. When both done: writes result.md, sends consolidated message to Ricardo
```

Both agents can be coordinator. The one who received Ricardo's request coordinates.

---

## AURA Changes Required

### 1. New endpoints on AURA FastAPI

File: `src/api/routers/agent_mesh.py` (new router)

```python
# POST /api/agent-query  — Hermes delegates task to AURA
# GET  /api/agent-status — Hermes reads AURA's full state
# POST /api/project/update — agent writes progress to shared project folder
```

### 2. Hermes-aware brain command
AURA's `/brain` or natural language: "dile a Hermes que..."
- AURA parses intent → delegates to Hermes via POST :18789
- Async response with 60s timeout
- Reports Hermes result to Ricardo

### 3. Dashboard panel: Hermes status
File: `dashboard/index.html`
- Add sidebar nav item "Hermes"
- Panel: Hermes health (GET :18789/health), active skills list, last heartbeat, mesh-log tail

---

## Hermes Model Stack — No API costs

| Priority | Model | Use case | Cost |
|---|---|---|---|
| 1 | ollama/qwen2.5:14b | Tool calling, day-to-day (~80% tasks) | $0 local |
| 2 | ollama/deepseek-r1:14b | Complex reasoning, planning | $0 local |
| 3 | nvidia/llama-3.3-70b | Heavy reasoning, long context | $0 nvapi free tier |
| 4 | Gemini CLI | Web search, URL analysis | $0 Google free |
| 5 | AURA proxy → Claude | Absolute maximum — via :8080 | $0 extra (Max sub) |

---

## Deployment

Both run as macOS LaunchAgents:
- `com.aura.telegram-bot` — already running
- `com.hermes.openclaw` — new, `openclaw start --daemon`

---

## Implementation Phases

### Phase 1 — Hermes standalone (1-2h)
- Install OpenClaw + Node 24
- Configure openclaw.json (Ollama + NVIDIA + Telegram)
- Write SOUL.md
- Install 5 core ClaHub skills
- Hermes responds on @rudserverbot

### Phase 2 — AURA bridge skill (1h)
- Write aura-bridge SKILL.md
- Add POST /api/agent-query to AURA's FastAPI
- Test: Ricardo asks Hermes → Hermes calls AURA → result back

### Phase 3 — AURA → Hermes delegation (1h)
- Add Hermes delegation to AURA brain router
- Natural language: "dile a Hermes que..."
- Test roundtrip both directions

### Phase 4 — Shared memory + heartbeat (30min)
- Hermes reads session-plan.md on heartbeat
- Both write to mesh-log.md
- Hermes.md updated after each session

### Phase 5 — Full cross-dashboard (2h)
- AURA dashboard "Hermes" panel: model, skills, crons, SOUL.md, heartbeat log, task queue
- Hermes `aura-context` skill: brain state, social queue, memory, roadmap, dashboard URL
- Both panels live-refresh every 30s

### Phase 6 — Shared projects (1h)
- `~/.aura/projects/` structure
- AURA `/api/project/update` endpoint
- Coordinator protocol in both agents
- Ricardo can say "lanza proyecto X" to either agent

---

## Success Criteria

- Ricardo messages Hermes → Hermes delegates to AURA → result in <30s
- Ricardo messages AURA → AURA delegates to Hermes → result in <30s
- "Lanza proyecto X" → both agents work in parallel → consolidated result to Ricardo
- AURA dashboard Hermes panel shows live: model, skills, crons, heartbeat
- Hermes `aura-context` skill returns AURA's full state on demand
- Hermes runs 24/7, heartbeat advances tasks from session-plan.md autonomously
- Zero Anthropic API charges from Hermes
- All 5 model tiers functional (Ollama → deepseek-r1 → NVIDIA 70B → Gemini → AURA proxy)
