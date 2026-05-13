"""Browser tool — headless Chromium via Playwright for JS-heavy pages and screenshots."""
from __future__ import annotations
import asyncio
import base64
from src.actions.registry import aura_tool


def _pw():
    """Import playwright — raises clear error if not installed."""
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError:
        raise RuntimeError("playwright not installed in this Python env. Run: playwright install chromium")


@aura_tool(
    name="browser_navigate",
    description="Open a URL in headless browser and return the page text/markdown. Works on JS-heavy SPAs that simple scrapers miss.",
    category="web",
    parameters={
        "url": {"type": "str", "description": "URL to open"},
        "wait_for": {"type": "str", "description": "CSS selector to wait for before extracting (optional)"},
        "extract": {"type": "str", "description": "CSS selector to extract (optional, default: body)"},
    },
)
async def browser_navigate(url: str, wait_for: str | None = None, extract: str | None = None) -> str:
    try:
        async_playwright = _pw()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=10000)
            selector = extract or "body"
            content = await page.inner_text(selector)
            await browser.close()
        return content[:6000]
    except Exception as e:
        return f"❌ browser_navigate error: {e}"


@aura_tool(
    name="browser_screenshot",
    description="Take a screenshot of a URL and return base64 PNG. Use to visually inspect pages or verify designs.",
    category="web",
    parameters={
        "url": {"type": "str", "description": "URL to screenshot"},
        "width": {"type": "int", "description": "Viewport width (default 1280)"},
        "height": {"type": "int", "description": "Viewport height (default 800)"},
        "full_page": {"type": "bool", "description": "Capture full page scroll (default False)"},
    },
)
async def browser_screenshot(url: str, width: int = 1280, height: int = 800, full_page: bool = False) -> str:
    try:
        async_playwright = _pw()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.goto(url, wait_until="networkidle", timeout=30000)
            png = await page.screenshot(full_page=full_page)
            await browser.close()
        b64 = base64.b64encode(png).decode()
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        return f"❌ browser_screenshot error: {e}"


@aura_tool(
    name="browser_click_and_extract",
    description="Navigate to a URL, click an element, then extract content. Good for tabs, accordions, login flows.",
    category="web",
    parameters={
        "url": {"type": "str", "description": "URL to open"},
        "click_selector": {"type": "str", "description": "CSS selector to click"},
        "extract_selector": {"type": "str", "description": "CSS selector to extract after click"},
    },
)
async def browser_click_and_extract(url: str, click_selector: str, extract_selector: str) -> str:
    try:
        async_playwright = _pw()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.click(click_selector)
            await page.wait_for_selector(extract_selector, timeout=8000)
            content = await page.inner_text(extract_selector)
            await browser.close()
        return content[:4000]
    except Exception as e:
        return f"❌ browser_click_and_extract error: {e}"
