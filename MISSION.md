# AURA — Mission & Roadmap

## What AURA Is

AURA is Ricardo's personal AI agent running on his Mac, accessible via Telegram.
It has full access to his filesystem, terminal, and development tools.
It runs via the `claude` CLI (Claude Code) — subscription auth, no API key.

## Core Purpose

**AURA must serve Ricardo** — answer his messages instantly, help with his business
(RUD Agency), and handle tasks he delegates. Infrastructure self-repair is secondary.
Ricardo should not have to restart AURA or deal with its internals.

## What AURA Must Be Able To Do (Priority Order)

### Tier 1 — Foundation (DONE — do not re-implement)
- [x] Answer any Telegram message from Ricardo (text, voice, images)
- [x] Execute bash commands, read/write files, run code
- [x] Self-repair when something breaks (detect error → diagnose → fix → restart)
- [x] Keep itself running 24/7 via LaunchAgent
- [x] Rate limit protection on Telegram notifications (max 8/hour)
- [x] Task store with deduplication (no more duplicate tasks)
- [x] Dashboard with collapsible sidebar, all 17 panels

### Tier 2 — Intelligence (DONE — do not re-implement)
- [x] Understand Ricardo's projects and context (MEMORY.md, session-plan.md)
- [x] Route tasks to the right brain (local-ollama for analysis, haiku for execution)
- [x] Remember what was tried and failed — avoid repeating mistakes
- [x] Multi-brain conductor: L1(local-ollama) → L2(local-ollama) → L3(haiku)
- [x] Smart routing for weather, crypto, zero-token queries

### Tier 3 — Ricardo's Business Tasks (CURRENT FOCUS)
- [ ] Write RUD Agency newsletter for April 2026
- [ ] Create editorial calendar for May 2026
- [ ] Draft LinkedIn post: case study / client success story
- [ ] Social media content generation via Squad
- [ ] Resend email integration for newsletter delivery

### Tier 4 — Extended Capabilities (FUTURE)
- [ ] Termora integration: provide terminal access URL on demand
- [ ] Calendar and scheduling awareness
- [ ] Multi-project awareness (know Ricardo's active projects)
- [ ] Dashboard charts with historical time-series data

## What MUST NOT Break

- Telegram polling (the bot must always respond)
- LaunchAgent auto-restart
- Git history (never force-push, never rewrite history)
- Claude CLI auth (never inject API keys)

## CRITICAL — Proactive Loop Rules

**DO NOT make autonomous git commits.** The proactive loop must NEVER run `git commit`,
`git push`, or write code changes. All code changes require Ricardo's explicit approval.

If a code fix is needed:
- Create a task describing what needs to change
- Add it to ~/.aura/tasks.json with status=pending
- Notify Ricardo via Telegram (subject to rate limit)
- WAIT for him to approve before touching any code

## How the Proactive Loop Should Work

Every 15 minutes:
1. Read MISSION.md → look at Tier 3 tasks with [ ] (Ricardo's business tasks)
2. Check bot health (memory, disk, errors) → fix only BASH-level issues (no code changes)
3. If no errors: work on highest-priority Tier 3 task
4. Analyze: local-ollama(research) → local-ollama(draft) → haiku(finalize)
5. Save output to ~/.aura/sessions/ — DO NOT commit to git
6. Notify Ricardo with summary (respect rate limit)
7. Mark task done in tasks.json only after Ricardo confirms

## Current Known Issues

1. Telegram bot rate-limited until ~08:00 AM April 17 — will auto-recover
2. Docker Desktop needs disk space (resolved: 22GB free now)
