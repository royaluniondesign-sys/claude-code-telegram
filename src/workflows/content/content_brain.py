"""Content Brain — Claude as strategist, cheap models as executors.

Daily flow:
  1. fetch_all() — pull fresh feed items (zero tokens)
  2. Claude (haiku) scores & selects 3 angles — minimal tokens
  3. Claude (haiku) writes full content plan per angle
  4. Saves plans to ~/.aura/content_plans/YYYY-MM-DD.json
  5. Notifies AURA via Telegram

Token budget per run: ~4k tokens total (haiku pricing).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic  # type: ignore[import-untyped]

from .feed_aggregator import FeedItem, fetch_all
from .content_memory import is_fresh, log_planned, recent_topics

log = logging.getLogger("content.brain")

PLANS_DIR = Path.home() / ".aura" / "content_plans"
RUD_VOICE_PATH = Path(__file__).parents[3] / "rud_voice.md"

# Models — haiku for speed/cost, sonnet only if needed
BRAIN_MODEL = "claude-haiku-4-5"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"

# Platforms and their formats
PLATFORMS = {
    "instagram": ["post_4_5", "carousel", "reel", "story"],
    "tiktok": ["reel"],
    "youtube_shorts": ["reel"],
    "linkedin": ["text_post"],
}

# Posting schedule — optimal windows (local time, 24h)
POST_SCHEDULE = {
    "post_4_5": {"days": [1, 3], "hour": 10},      # Mon, Wed 10am
    "carousel": {"days": [2, 4], "hour": 17},       # Tue, Thu 5pm
    "reel": {"days": [1, 3, 5], "hour": 19},        # Mon, Wed, Fri 7pm
    "story": {"days": [0, 1, 2, 3, 4], "hour": 9}, # Weekdays 9am
    "text_post": {"days": [1, 3], "hour": 11},      # Mon, Wed 11am
}


def _load_voice() -> str:
    try:
        return RUD_VOICE_PATH.read_text(encoding="utf-8")
    except Exception:
        return "RUD Studio — premium branding agency. Sharp, opinionated, design-forward."


def _items_to_digest(items: list[FeedItem]) -> str:
    """Compress feed items to a short digest for the LLM."""
    lines = []
    for i, item in enumerate(items[:60]):  # max 60 items
        lines.append(
            f"{i+1}. [{item.source} / {item.pillar}] {item.title}"
            + (f" — {item.summary[:80]}" if item.summary else "")
        )
    return "\n".join(lines)


def _recent_topics_digest() -> str:
    topics = recent_topics(20)
    if not topics:
        return "No recent content yet."
    lines = [f"- {t['title']} ({t['format']} · {t['status']})" for t in topics[:10]]
    return "\n".join(lines)


async def _call_claude(prompt: str, system: str, max_tokens: int = 1200) -> str:
    """Call Claude Haiku — cheap + fast."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        # Fallback: try OpenRouter
        return await _call_openrouter(prompt, system, max_tokens)

    client = anthropic.Anthropic(api_key=key)
    loop = asyncio.get_event_loop()

    def _do():
        msg = client.messages.create(
            model=BRAIN_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    try:
        return await loop.run_in_executor(None, _do)
    except Exception as e:
        log.warning("claude_brain_error: %s", e)
        return await _call_openrouter(prompt, system, max_tokens)


async def _call_openrouter(prompt: str, system: str, max_tokens: int = 1200) -> str:
    """Fallback to OpenRouter free models — zero cost, with retry + model cascade."""
    import httpx

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("No AI key available")

    # Model cascade: try each in order until one responds
    models = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-120b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "meta-llama/llama-3.2-3b-instruct:free",
    ]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://rud.studio",
        "X-Title": "RUD Content Brain",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        for i, model in enumerate(models):
            try:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json={"model": model, "messages": messages, "max_tokens": max_tokens},
                )
                if r.status_code == 429:
                    wait = 5 * (i + 1)
                    log.warning("openrouter_429 model=%s waiting=%ds", model, wait)
                    await asyncio.sleep(wait)
                    # retry same model once
                    r = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=headers,
                        json={"model": model, "messages": messages, "max_tokens": max_tokens},
                    )
                if r.status_code == 200:
                    data = r.json()
                    return data["choices"][0]["message"]["content"]
                log.warning("openrouter_error model=%s status=%s", model, r.status_code)
            except Exception as e:
                log.warning("openrouter_model_failed model=%s error=%s", model, e)
                continue

    raise RuntimeError("All OpenRouter models failed")


async def select_topics(items: list[FeedItem]) -> list[dict]:
    """Claude selects 3 content angles from feed digest. Returns list of dicts."""
    voice = _load_voice()
    digest = _items_to_digest(items)
    recent = _recent_topics_digest()

    system = f"""You are the Content Strategist for RUD Studio.
Your job: select 3 content angles that are genuinely interesting, timely, and fit RUD's voice.

RUD VOICE & CONTEXT:
{voice}

RECENT CONTENT (avoid repetition):
{recent}

OUTPUT FORMAT — respond ONLY with valid JSON array, no markdown:
[
  {{
    "topic_key": "short-slug-no-spaces",
    "headline": "The actual post headline/hook (max 12 words, strong opinion or insight)",
    "angle": "What's the specific POV or insight RUD adds to this topic",
    "source_items": [1, 4],
    "pillar": "branding|ai|design|advertising|typography|marketing",
    "format": "post_4_5|carousel|reel|story|text_post",
    "platforms": ["instagram", "tiktok"],
    "why_now": "Why this is timely (1 sentence)",
    "avoid": "What NOT to do with this topic (the generic version)"
  }}
]

Rules:
- Pick topics with strong POV, not neutral reporting
- Prioritize AI × Design intersections — RUD's sweet spot
- No motivational content, no generic tips lists
- Select different formats (not all posts)
- If a topic was done recently, skip it
"""

    prompt = f"""Today's feed digest ({len(items)} items from last 48h):

{digest}

Select the 3 best content angles for RUD Studio this week. Return JSON only."""

    raw = await _call_claude(prompt, system, max_tokens=900)

    # Parse JSON — extract from any wrapper text
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        return json.loads(raw[start:end])
    except Exception as e:
        log.error("topic_parse_error: %s | raw: %s", e, raw[:200])
        return []


async def generate_content_plan(angle: dict, feed_items: list[FeedItem]) -> dict:
    """Claude writes the full content plan for one angle. Returns enriched dict."""
    voice = _load_voice()

    # Find source items
    source_texts = []
    for idx in angle.get("source_items", []):
        if 0 < idx <= len(feed_items):
            item = feed_items[idx - 1]
            source_texts.append(f"- {item.title}: {item.summary[:150]}")

    fmt = angle.get("format", "post_4_5")
    platforms = angle.get("platforms", ["instagram"])

    system = f"""You are RUD Studio's Creative Director writing a content brief.
RUD VOICE: {voice[:800]}

Be sharp, opinionated, and specific. No filler. No generic advice.
Respond ONLY with valid JSON, no markdown wrapper."""

    prompt = f"""Write the full production brief for this content:

HEADLINE: {angle['headline']}
ANGLE: {angle['angle']}
FORMAT: {fmt}
PLATFORMS: {', '.join(platforms)}
WHY NOW: {angle['why_now']}
AVOID: {angle['avoid']}

SOURCE CONTEXT:
{chr(10).join(source_texts) or 'General industry knowledge'}

Return JSON:
{{
  "headline": "final hook headline",
  "subhead": "supporting line (max 10 words)",
  "body_copy": "Full caption/script text (platform-appropriate length). For reels: describe each scene. For carousels: text per slide as array.",
  "hashtags": ["max", "15", "relevant", "hashtags"],
  "cta": "Call to action (save, comment, share — be specific)",
  "visual_brief": "Detailed visual direction for FLUX/open-design generation",
  "color_palette": "Which RUD colors + specific usage",
  "typography_notes": "Font choices and text treatment",
  "posting_time": "Best time to post this format",
  "slides": {{"count": 1, "texts": []}},
  "remotion_script": "For reels: scene-by-scene text animation script (null for static posts)",
  "linkedin_copy": "Adapted professional copy for LinkedIn (null if not for LinkedIn)"
}}"""

    raw = await _call_claude(prompt, system, max_tokens=1400)

    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        plan = json.loads(raw[start:end])
    except Exception as e:
        log.error("plan_parse_error: %s", e)
        plan = {"headline": angle["headline"], "body_copy": "", "hashtags": []}

    # Merge angle metadata into plan
    plan.update({
        "topic_key": angle.get("topic_key", ""),
        "format": fmt,
        "platforms": platforms,
        "pillar": angle.get("pillar", "design"),
        "angle": angle.get("angle", ""),
        "why_now": angle.get("why_now", ""),
    })
    return plan


def _next_post_datetime(fmt: str) -> str:
    """Calculate next optimal posting time for this format."""
    now = datetime.now(timezone.utc)
    sched = POST_SCHEDULE.get(fmt, {"days": [1, 3], "hour": 12})
    # Find next available day
    for offset in range(8):
        candidate = now.replace(hour=sched["hour"], minute=0, second=0, microsecond=0)
        candidate = candidate.replace(day=now.day + offset)
        if candidate.weekday() in sched["days"] and candidate > now:
            return candidate.isoformat()
    return now.isoformat()


async def run_daily_brain() -> dict:
    """Main entry point — runs the full daily content planning cycle.

    Returns summary dict for Telegram notification.
    """
    log.info("content_brain_start")
    PLANS_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Fetch feeds (zero tokens)
    items = await fetch_all()
    if not items:
        return {"ok": False, "error": "No feed items fetched", "plans": []}

    # Step 2: Select topics (cheap Haiku call)
    angles = await select_topics(items)
    if not angles:
        return {"ok": False, "error": "Topic selection failed", "plans": []}

    # Step 3: Filter already-seen topics
    fresh_angles = [a for a in angles if is_fresh(a.get("topic_key", ""))]
    if not fresh_angles:
        fresh_angles = angles  # Override if all were "seen" (shouldn't happen)

    # Step 4: Generate full plans (one Haiku call per angle)
    plans = []
    for angle in fresh_angles[:3]:
        try:
            plan = await generate_content_plan(angle, items)
            plan["scheduled_at"] = _next_post_datetime(plan.get("format", "post_4_5"))
            plan["status"] = "planned"
            plan["created_at"] = datetime.now(timezone.utc).isoformat()

            # Save to memory DB
            row_id = log_planned(
                topic_key=plan.get("topic_key", angle.get("topic_key", "")),
                title=plan.get("headline", ""),
                fmt=plan.get("format", "post_4_5"),
                platform=",".join(plan.get("platforms", ["instagram"])),
                meta={"angle": plan.get("angle", ""), "scheduled_at": plan.get("scheduled_at")},
            )
            plan["memory_id"] = row_id
            plans.append(plan)
            log.info("plan_created topic=%s fmt=%s", plan.get("topic_key"), plan.get("format"))
        except Exception as e:
            log.error("plan_generation_error: %s", e)

    # Step 5: Save plans to disk
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan_file = PLANS_DIR / f"{today}.json"
    plan_file.write_text(
        json.dumps({"date": today, "feed_count": len(items), "plans": plans},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("plans_saved path=%s count=%d", plan_file, len(plans))

    return {
        "ok": True,
        "date": today,
        "feed_items": len(items),
        "plans": plans,
        "plan_file": str(plan_file),
    }


def load_today_plans() -> list[dict]:
    """Load today's content plans from disk."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan_file = PLANS_DIR / f"{today}.json"
    if not plan_file.exists():
        return []
    try:
        data = json.loads(plan_file.read_text())
        return data.get("plans", [])
    except Exception:
        return []


def load_plans_for_date(date_str: str) -> list[dict]:
    plan_file = PLANS_DIR / f"{date_str}.json"
    if not plan_file.exists():
        return []
    try:
        return json.loads(plan_file.read_text()).get("plans", [])
    except Exception:
        return []
