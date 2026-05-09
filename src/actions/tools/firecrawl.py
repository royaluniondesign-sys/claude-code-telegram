"""Firecrawl tool — web scraping + crawling with AI-ready markdown output."""
from __future__ import annotations
import os
from firecrawl import FirecrawlApp
from src.actions.registry import aura_tool

_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "fc-302b6f703a9b48ee85257f65be12d8d0")


def _app() -> FirecrawlApp:
    return FirecrawlApp(api_key=_API_KEY)


@aura_tool(
    name="firecrawl_scrape",
    description="Scrape a URL and return clean markdown content. Great for reading articles, docs, product pages.",
    category="web",
    parameters={
        "url": {"type": "str", "description": "URL to scrape"},
        "formats": {"type": "list", "description": "Output formats: ['markdown', 'html', 'links']. Default: ['markdown']"},
    },
)
async def firecrawl_scrape(url: str, formats: list | None = None) -> str:
    import asyncio
    formats = formats or ["markdown"]
    result = await asyncio.to_thread(_app().scrape_url, url, params={"formats": formats})
    if hasattr(result, "markdown") and result.markdown:
        return result.markdown[:6000]
    return str(result)[:6000]


@aura_tool(
    name="firecrawl_crawl",
    description="Crawl an entire website and return all pages as markdown. Use for docs sites, blogs, multi-page content.",
    category="web",
    parameters={
        "url": {"type": "str", "description": "Root URL to crawl"},
        "max_pages": {"type": "int", "description": "Max pages to crawl (default 10)"},
    },
)
async def firecrawl_crawl(url: str, max_pages: int = 10) -> str:
    import asyncio
    result = await asyncio.to_thread(
        _app().crawl_url, url,
        params={"crawlerOptions": {"limit": max_pages}, "pageOptions": {"onlyMainContent": True}}
    )
    pages = result if isinstance(result, list) else result.get("data", [])
    output = []
    for p in pages[:max_pages]:
        page_url = p.get("url", "") if isinstance(p, dict) else getattr(p, "url", "")
        markdown = p.get("markdown", "") if isinstance(p, dict) else getattr(p, "markdown", "")
        if markdown:
            output.append(f"## {page_url}\n{markdown[:1500]}")
    return "\n\n---\n\n".join(output)[:8000] or "No content found"


@aura_tool(
    name="firecrawl_search",
    description="Search the web and return scraped content from top results.",
    category="web",
    parameters={
        "query": {"type": "str", "description": "Search query"},
        "limit": {"type": "int", "description": "Number of results (default 5)"},
    },
)
async def firecrawl_search(query: str, limit: int = 5) -> str:
    import asyncio
    result = await asyncio.to_thread(_app().search, query, params={"limit": limit})
    items = result if isinstance(result, list) else result.get("data", [])
    output = []
    for item in items[:limit]:
        title = item.get("title", "") if isinstance(item, dict) else getattr(item, "title", "")
        url = item.get("url", "") if isinstance(item, dict) else getattr(item, "url", "")
        markdown = item.get("markdown", "") if isinstance(item, dict) else getattr(item, "markdown", "")
        output.append(f"**{title}**\n{url}\n{markdown[:800]}")
    return "\n\n---\n\n".join(output)[:6000] or "No results"
