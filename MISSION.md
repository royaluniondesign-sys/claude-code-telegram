# AURA — Mission & Roadmap

## What AURA Is

AURA is Ricardo's personal AI agent running on his Mac, accessible via Telegram.
It has full access to his filesystem, terminal, and development tools.
It runs via the `claude` CLI (Claude Code) — subscription auth, no API key.

## Core Purpose

**AURA must become fully autonomous** — able to run, monitor, repair, and improve itself
without Ricardo having to intervene. Ricardo should only need to talk to it via Telegram.

## What AURA Must Be Able To Do (Priority Order)

### Tier 1 — Foundation (must work 100% reliably)
- [x] Answer any Telegram message from Ricardo (text, voice, images)
- [x] Execute bash commands, read/write files, run code
- [x] Self-repair when something breaks (detect error → diagnose → fix → restart)
- [x] Keep itself running 24/7 via LaunchAgent

### Tier 2 — Intelligence
- [ ] Understand Ricardo's projects and context (MEMORY.md, session-plan.md)
- [ ] Route tasks to the right brain (local-ollama for analysis, haiku for execution)
- [ ] Remember what was tried and failed — avoid repeating mistakes
- [ ] Escalate to sonnet when haiku/ollama fail 3 times

### Tier 3 — Autonomous Development
- [x] Generate strategic improvement tasks based on this MISSION.md
- [ ] Build features in priority order (Tier 1 → Tier 2 → Tier 3)
- [x] Run tests after every commit and repair broken tests automatically
- [x] Write learnings to ~/.aura/memory/ after each conductor run

### Tier 4 — Extended Capabilities
- [ ] Termora integration: provide terminal access URL on demand
- [ ] Email integration via Resend
- [ ] Calendar and scheduling awareness
- [ ] Multi-project awareness (know Ricardo's active projects)

## What MUST NOT Break

- Telegram polling (the bot must always respond)
- LaunchAgent auto-restart
- Git history (never force-push, never rewrite history)
- Claude CLI auth (never inject API keys)

## Current Known Gaps (as of today)

1. Auto-generated tasks are random — not strategic (fix: read this file in _generate_new_tasks)
2. No post-commit test verification
3. No learning memory written after runs
4. Rate limit triggered when conductor sends too many Telegram messages
5. Dashboard tunnel URL changes on restart

## How the Proactive Loop Should Work

Every 15 minutes:
1. Read MISSION.md → understand what to build next
2. Check bot logs for errors → repair first if any
3. If no errors: pick highest-priority uncompleted item from this mission
4. Build it: local-ollama(diagnose) → local-ollama(codegen) → haiku(write+commit)
5. After commit: run syntax check + tests
6. Write learning to ~/.aura/memory/conductor_log.md
7. Update MISSION.md checkbox if item completed