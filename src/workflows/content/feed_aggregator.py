"""Feed aggregator — pulls daily signals from design/AI/branding sources.

Returns a flat list of fresh items (title, url, summary, source, published)
sorted by recency. Uses only RSS/Atom — no scraping, no JS rendering needed.
Zero LLM tokens consumed here.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import urllib.request
import feedparser  # type: ignore[import-untyped]

log = logging.getLogger("content.feeds")

# ── Feed catalog ─────────────────────────────────────────────────────────────
FEEDS: list[dict] = [
    # Design & Branding
    {"url": "https://www.underconsideration.com/brandnew/atom.xml",
     "source": "Brand New", "pillar": "branding"},
    {"url": "https://the-brandidentity.com/feed",
     "source": "The Brand Identity", "pillar": "branding"},
    {"url": "https://fontsinuse.com/feed",
     "source": "Fonts In Use", "pillar": "typography"},
    {"url": "https://www.creativebloq.com/feeds/all",
     "source": "Creative Bloq", "pillar": "design"},
    {"url": "https://uxdesign.cc/feed",
     "source": "UX Collective", "pillar": "design"},
    {"url": "https://adsoftheworld.com/rss",
     "source": "Ads of the World", "pillar": "advertising"},
    # AI & Tech
    {"url": "https://www.anthropic.com/rss.xml",
     "source": "Anthropic Blog", "pillar": "ai"},
    {"url": "https://news.ycombinator.com/rss",
     "source": "Hacker News", "pillar": "tech"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/",
     "source": "TechCrunch AI", "pillar": "ai"},
    {"url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
     "source": "The Verge AI", "pillar": "ai"},
    {"url": "https://venturebeat.com/ai/feed/",
     "source": "VentureBeat AI", "pillar": "ai"},
    # Marketing & Digital
    {"url": "https://digiday.com/feed/",
     "source": "Digiday", "pillar": "marketing"},
    {"url": "https://www.fastcompany.com/design/rss",
     "source": "Fast Company Design", "pillar": "design"},
    {"url": "https://www.marketingweek.com/feed/",
     "source": "Marketing Week", "pillar": "marketing"},
    # Product launches
    {"url": "https://www.producthunt.com/feed",
     "source": "Product Hunt", "pillar": "tech"},
]

# Filter HN to only design/AI relevant items via keyword
HN_KEYWORDS = {
    "design", "ai", "llm", "brand", "typography", "visual", "figma",
    "ux", "ui", "logo", "identity", "claude", "openai", "anthropic",
    "marketing", "creative", "agency", "advertising",
}

MAX_AGE_HOURS = 48  # Only items published in last 48h


@dataclass
class FeedItem:
    title: str
    url: str
    summary: str
    source: str
    pillar: str
    published: datetime
    score: float = 0.0  # Filled by scorer later
    extra: dict = field(default_factory=dict)


def _parse_date(entry: dict) -> datetime:
    """Best-effort date parsing from feedparser entry."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(attr)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _fetch_feed(feed_cfg: dict, cutoff: datetime) -> list[FeedItem]:
    """Fetch a single RSS feed. Returns items newer than cutoff."""
    items: list[FeedItem] = []
    try:
        parsed = feedparser.parse(
            feed_cfg["url"],
            request_headers={"User-Agent": "RUD-ContentBot/1.0"},
        )
        for entry in parsed.entries[:30]:  # max 30 per feed
            pub = _parse_date(entry)
            if pub < cutoff:
                continue
            title = (entry.get("title") or "").strip()
            url = entry.get("link") or entry.get("url") or ""
            summary = (
                entry.get("summary") or entry.get("description") or ""
            )[:300].strip()

            if not title or not url:
                continue

            # HN keyword filter
            if feed_cfg["source"] == "Hacker News":
                title_lower = title.lower()
                if not any(kw in title_lower for kw in HN_KEYWORDS):
                    continue

            items.append(FeedItem(
                title=title,
                url=url,
                summary=summary,
                source=feed_cfg["source"],
                pillar=feed_cfg["pillar"],
                published=pub,
            ))
    except Exception as e:
        log.warning("feed_error source=%s: %s", feed_cfg["source"], e)
    return items


async def fetch_all(max_age_hours: int = MAX_AGE_HOURS) -> list[FeedItem]:
    """Fetch all feeds concurrently. Returns deduplicated items sorted by recency."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    loop = asyncio.get_event_loop()

    tasks = [
        loop.run_in_executor(None, _fetch_feed, cfg, cutoff)
        for cfg in FEEDS
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen_titles: set[str] = set()
    all_items: list[FeedItem] = []
    for r in results:
        if isinstance(r, list):
            for item in r:
                key = item.title.lower()[:60]
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_items.append(item)

    # Sort newest first
    all_items.sort(key=lambda x: x.published, reverse=True)
    log.info("feeds_fetched total=%d cutoff_h=%d", len(all_items), max_age_hours)
    return all_items
