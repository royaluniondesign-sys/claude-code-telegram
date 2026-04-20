"""Blog Publisher — publishes posts to rud-web.vercel.app via GitHub API.

Flow:
  1. Generate content with AI (Gemini / local Ollama)
  2. Fetch current blog page.tsx + [slug]/page.tsx from GitHub
  3. Inject new post into both files
  4. Commit both files → Vercel auto-deploys in ~30s
  5. Return published URL

No CMS needed — GitHub API is the CMS.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import structlog

logger = structlog.get_logger()

_GH_API = "https://api.github.com"
_REPO = os.environ.get("GITHUB_REPO_BLOG", "royaluniondesign-sys/RUD-WEB")
_BLOG_PAGE = "src/app/blog/page.tsx"
_BLOG_SLUG = "src/app/blog/[slug]/page.tsx"
_BLOG_BASE_URL = "https://rud-web.vercel.app/blog"

# Unsplash images by category (curated, no API needed)
_CATEGORY_IMAGES = {
    "ia": "https://images.unsplash.com/photo-1677442135703-1787eea5ce01?w=900&q=85",
    "branding": "https://images.unsplash.com/photo-1558655146-9f40138edfeb?w=900&q=85",
    "diseño": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=900&q=85",
    "ecommerce": "https://images.unsplash.com/photo-1661956602116-aa6865609028?w=900&q=85",
    "automatización": "https://images.unsplash.com/photo-1611532736597-de2d4265fba3?w=900&q=85",
    "estrategia": "https://images.unsplash.com/photo-1542744173-8e7e53415bb0?w=900&q=85",
    "web": "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=900&q=85",
    "default": "https://images.unsplash.com/photo-1497366216548-37526070297c?w=900&q=85",
}


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _pick_image(category: str) -> str:
    cat_lower = category.lower()
    for key, url in _CATEGORY_IMAGES.items():
        if key in cat_lower:
            return url
    return _CATEGORY_IMAGES["default"]


def _slugify(title: str) -> str:
    """Convert title to URL slug."""
    import unicodedata
    # Normalize and remove accents
    normalized = unicodedata.normalize("NFD", title.lower())
    ascii_str = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    # Replace spaces and special chars with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_str)
    return slug.strip("-")[:60]


def _estimate_read_time(content: str) -> str:
    words = len(content.split())
    minutes = max(3, round(words / 200))
    return f"{minutes} min"


@dataclass
class BlogPost:
    slug: str
    title: str
    date: str
    category: str
    read_time: str
    image: str
    excerpt: str
    content: str


async def _gh_get(session: aiohttp.ClientSession, path: str) -> dict:
    """GET a file from GitHub API, returns {content, sha}."""
    headers = {
        "Authorization": f"token {_token()}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with session.get(
        f"{_GH_API}/repos/{_REPO}/contents/{path}",
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"GitHub GET {path} failed {resp.status}: {text[:200]}")
        return await resp.json()


async def _gh_put(
    session: aiohttp.ClientSession,
    path: str,
    content: str,
    sha: str,
    message: str,
) -> dict:
    """PUT (update) a file on GitHub API."""
    headers = {
        "Authorization": f"token {_token()}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "committer": {"name": "AURA", "email": "aura@rud-web.vercel.app"},
    }
    async with session.put(
        f"{_GH_API}/repos/{_REPO}/contents/{path}",
        headers=headers,
        json=body,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status not in (200, 201):
            text = await resp.text()
            raise RuntimeError(f"GitHub PUT {path} failed {resp.status}: {text[:300]}")
        return await resp.json()


def _inject_post_to_page_tsx(tsx_content: str, post: BlogPost) -> str:
    """Add post entry to the posts[] array in blog/page.tsx."""
    new_entry = f"""  {{
    slug: '{post.slug}',
    title: '{post.title.replace("'", "\\'")}',
    date: '{post.date}', category: '{post.category}', readTime: '{post.read_time}',
    image: '{post.image}',
    excerpt: '{post.excerpt.replace("'", "\\'")}',
    featured: false,
  }},"""

    # Insert as the FIRST post in the array (so it appears at top/featured)
    # Find "const posts = [" and insert after it
    pattern = r"(const posts = \[)"
    replacement = r"\1\n" + new_entry
    updated = re.sub(pattern, replacement, tsx_content, count=1)

    if updated == tsx_content:
        # Fallback: find the first post's opening brace in the array
        logger.warning("blog_inject_fallback", path=_BLOG_PAGE)
        # Try to find "const posts = [" more loosely
        idx = tsx_content.find("const posts = [")
        if idx >= 0:
            insert_at = tsx_content.index("[", idx) + 1
            updated = tsx_content[:insert_at] + "\n" + new_entry + tsx_content[insert_at:]

    return updated


def _inject_post_to_slug_tsx(tsx_content: str, post: BlogPost) -> str:
    """Add post content to POSTS dict in blog/[slug]/page.tsx."""
    # Escape content for JS template literal
    content_escaped = (
        post.content
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )

    new_entry = f"""  '{post.slug}': {{
    title: '{post.title.replace("'", "\\'")}',
    date: '{post.date}', category: '{post.category}', readTime: '{post.read_time}',
    image: '{post.image}',
    excerpt: '{post.excerpt.replace("'", "\\'")}',
    content: `{content_escaped}`,
  }},"""

    # Find "const POSTS: Record<..." and insert after the opening {
    pattern = r"(const POSTS[^{]*\{)"
    replacement = r"\1\n" + new_entry
    updated = re.sub(pattern, replacement, tsx_content, count=1)

    if updated == tsx_content:
        logger.warning("blog_slug_inject_fallback", path=_BLOG_SLUG)
        idx = tsx_content.find("const POSTS")
        if idx >= 0:
            brace_idx = tsx_content.index("{", idx) + 1
            updated = tsx_content[:brace_idx] + "\n" + new_entry + tsx_content[brace_idx:]

    return updated


async def generate_blog_content(topic: str) -> BlogPost:
    """Use Gemini CLI to generate structured blog post content."""
    import asyncio

    # Build date string
    from datetime import datetime
    months_es = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                  "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    now = datetime.now()
    date_str = f"{months_es[now.month - 1]} {now.year}"

    # Categorize topic
    topic_lower = topic.lower()
    if any(w in topic_lower for w in ["ia", "ai", "inteligencia", "llm", "modelo", "aura", "automatiz"]):
        category = "IA & Tecnología"
        img_key = "ia"
    elif any(w in topic_lower for w in ["brand", "marca", "identidad", "logo", "visual"]):
        category = "Branding"
        img_key = "branding"
    elif any(w in topic_lower for w in ["shop", "ecommerce", "tienda", "venta"]):
        category = "E-commerce"
        img_key = "ecommerce"
    elif any(w in topic_lower for w in ["web", "next", "react", "frontend", "desarrollo"]):
        category = "Desarrollo Web"
        img_key = "web"
    elif any(w in topic_lower for w in ["n8n", "workflow", "pipeline", "automatiza"]):
        category = "Automatización"
        img_key = "automatización"
    else:
        category = "Estrategia"
        img_key = "estrategia"

    prompt = f"""Genera un artículo de blog para RUD Studio (agencia creativa en Barcelona).

Tema: {topic}
Categoría: {category}
Tono: Directo, práctico, sin relleno. Voz de agencia que sabe lo que hace.
Audiencia: Empresas y startups españolas que buscan branding + web + IA.
Longitud: 600-900 palabras de contenido real.

Responde EXACTAMENTE en este formato JSON (sin markdown, sin ```):
{{
  "title": "título del artículo (max 80 chars, atractivo, con keyword)",
  "excerpt": "resumen de 1-2 frases (max 200 chars, con gancho)",
  "content": "cuerpo completo del artículo (párrafos separados por doble salto de línea, sin markdown headers, texto puro)"
}}"""

    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        raw = stdout.decode("utf-8").strip()

        # Try to parse JSON from response
        json_match = re.search(r'\{[^{}]*"title"[^{}]*"excerpt"[^{}]*"content".*\}', raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
        else:
            # Try to find JSON object in response
            try:
                data = json.loads(raw)
            except Exception:
                # Fallback: extract manually
                title_m = re.search(r'"title"\s*:\s*"([^"]+)"', raw)
                excerpt_m = re.search(r'"excerpt"\s*:\s*"([^"]+)"', raw)
                content_m = re.search(r'"content"\s*:\s*"(.*)"', raw, re.DOTALL)
                data = {
                    "title": title_m.group(1) if title_m else topic,
                    "excerpt": excerpt_m.group(1) if excerpt_m else topic,
                    "content": content_m.group(1).replace("\\n", "\n") if content_m else raw[:1000],
                }

        title = data["title"].strip()
        excerpt = data["excerpt"].strip()[:250]
        content = data["content"].strip()

    except Exception as e:
        logger.error("blog_ai_generation_failed", error=str(e))
        # Fallback to a basic structure
        title = topic
        excerpt = f"Perspectiva de RUD Studio sobre {topic}."
        content = f"[CONTENIDO PENDIENTE — Error en generación: {e}]"

    slug = _slugify(title)
    read_time = _estimate_read_time(content)
    image = _pick_image(img_key)

    return BlogPost(
        slug=slug,
        title=title,
        date=date_str,
        category=category,
        read_time=read_time,
        image=image,
        excerpt=excerpt,
        content=content,
    )


async def publish_blog_post(
    post: BlogPost,
) -> dict:
    """Publish a blog post to rud-web.vercel.app via GitHub API.

    Returns:
        {"ok": True, "url": str, "slug": str, "commit_sha": str}
        or {"ok": False, "error": str}
    """
    if not _token():
        return {"ok": False, "error": "GITHUB_TOKEN not set in .env"}

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Fetch current files
            logger.info("blog_publish_start", slug=post.slug)
            page_data = await _gh_get(session, _BLOG_PAGE)
            slug_data = await _gh_get(session, _BLOG_SLUG)

            page_content = base64.b64decode(page_data["content"]).decode("utf-8")
            slug_content = base64.b64decode(slug_data["content"]).decode("utf-8")

            # 2. Inject post
            updated_page = _inject_post_to_page_tsx(page_content, post)
            updated_slug = _inject_post_to_slug_tsx(slug_content, post)

            commit_msg = f"feat(blog): {post.title[:60]} [{post.date}]"

            # 3. Commit page.tsx
            page_result = await _gh_put(
                session, _BLOG_PAGE, updated_page,
                page_data["sha"], commit_msg,
            )

            # 4. Commit slug/page.tsx (get new sha after first commit)
            slug_page_data = await _gh_get(session, _BLOG_SLUG)
            await _gh_put(
                session, _BLOG_SLUG, updated_slug,
                slug_page_data["sha"], f"feat(blog): content for {post.slug}",
            )

            commit_sha = page_result.get("commit", {}).get("sha", "")[:7]
            post_url = f"{_BLOG_BASE_URL}/{post.slug}"

            logger.info("blog_publish_done", url=post_url, sha=commit_sha)
            return {
                "ok": True,
                "url": post_url,
                "slug": post.slug,
                "title": post.title,
                "commit_sha": commit_sha,
                "deploy_note": "Vercel desplegará automáticamente en ~30-60s",
            }

    except Exception as e:
        logger.error("blog_publish_error", error=str(e))
        return {"ok": False, "error": str(e)}


async def publish_blog_from_topic(topic: str) -> dict:
    """Full pipeline: generate content + publish.

    Returns result dict ready to send as Telegram message.
    """
    logger.info("blog_pipeline_start", topic=topic)
    post = await generate_blog_content(topic)
    result = await publish_blog_post(post)
    result["post"] = {
        "title": post.title,
        "category": post.category,
        "date": post.date,
        "slug": post.slug,
        "excerpt": post.excerpt,
    }
    return result
