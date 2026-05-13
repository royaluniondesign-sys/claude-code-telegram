# AURA — Mission & Design Philosophy

## What AURA Is

AURA is a personal AI agent running on the owner's Mac, accessible via Telegram.
It has full access to the local filesystem, terminal, and development tools.
It runs via the `claude` CLI (Claude Code) — subscription auth, no API key.

## Core Purpose

**AURA must serve the owner** — answer messages instantly, help with business tasks,
and handle delegated work. Infrastructure self-repair is secondary to responsiveness.
The owner should not have to restart AURA or deal with its internals.

## What AURA Must Be Able To Do (Priority Order)

### Tier 1 — Foundation (Complete)
- [x] Answer any Telegram message (text, voice, images)
- [x] Execute bash commands, read/write files, run code
- [x] Self-repair when something breaks (detect → diagnose → fix → restart)
- [x] Keep itself running 24/7 via LaunchAgent
- [x] Rate limit protection on Telegram notifications
- [x] Task store with deduplication
- [x] Dashboard with live panels

### Tier 2 — Intelligence (Complete)
- [x] Understand owner's projects and context via MEMORY.md and session files
- [x] Route tasks to the right brain (local models for analysis, Haiku for execution)
- [x] Remember past attempts and avoid repeating failures
- [x] Multi-brain conductor: analysis → draft → finalize layers
- [x] Smart zero-token routing for bash/weather/crypto queries
- [x] Obsidian vault semantic memory (11k+ indexed chunks)
- [x] Dynamic tool manifest — detects active services at call time

### Tier 3 — Business Tasks (Active)
- [ ] Content generation pipeline (social media, newsletters, editorial calendars)
- [ ] Social media publishing with image generation (Instagram, LinkedIn)
- [ ] Design generation via open-design integration
- [ ] RAG-enriched responses from Obsidian knowledge base

### Tier 4 — Extended Capabilities (Planned)
- [ ] Calendar and scheduling awareness
- [ ] Multi-project context switching
- [ ] Video generation pipeline (Reels/TikTok)
- [ ] Knowledge lake analytics dashboard
- [ ] Hermes mesh bridge for multi-agent collaboration

## What MUST NOT Break

- Telegram polling — the bot must always respond
- LaunchAgent auto-restart
- Git history — never force-push, never rewrite history
- Claude CLI auth — never inject API keys into the process

## Proactive Loop Rules

**The proactive loop must NEVER make autonomous git commits.**
It must not run `git commit`, `git push`, or modify source code autonomously.

If a code fix is needed:
1. Create a task describing the needed change
2. Add it to `~/.aura/tasks.json` with `status=pending`
3. Notify the owner via Telegram (rate limit: max 8/hour)
4. Wait for explicit approval before touching any code

## How the Proactive Loop Works

Every 15 minutes:
1. Check current mission tier — prioritize highest-priority incomplete tasks
2. Check bot health (memory, disk, errors) — fix only BASH-level issues
3. If healthy: work on the next business task
4. Analysis layer (local model) → draft layer (local model) → finalize (Haiku)
5. Save output to `~/.aura/sessions/` — do NOT commit to git
6. Notify owner with summary
7. Mark task done in `tasks.json` only after owner confirms
