"""Social Publisher — unified interface for Instagram + Facebook posting.

Instagram: Meta Graph API (System User token, instagram_content_publish scope)
Facebook: Meta Graph API (Page access token via same System User)

IMPORTANT — One-time manual setup needed (M3):
  The INSTAGRAM_ACCOUNT_ID in .env may be wrong/disconnected.
  To fix: Meta Business Manager → Business Settings → Instagram Accounts
  → Add account → assign to System User AURA.
  Then get the correct IG Business Account ID and update INSTAGRAM_ACCOUNT_ID.

Architecture:
  1. Generate caption with AI
  2. Generate image with FLUX.1 (pollinations.ai, free, no API key)
  3. Upload image to temp host (0x0.st)
  4. POST to Instagram/Facebook Graph API
  5. Return post URL or save as draft on error

Commands:
  - /post instagram [description]
  - /post facebook [description]
  - /post social [description]  ← posts to both
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import aiohttp
import structlog

logger = structlog.get_logger()

_GRAPH_BASE = "https://graph.facebook.com/v22.0"
_DRAFTS_DIR = Path.home() / ".aura" / "social_drafts"
_POLLS_BASE = "https://image.pollinations.ai/prompt"

# Minimal gemini config without MCP servers — prevents 80-second AURA MCP startup overhead
_GEMINI_NO_MCP_HOME = Path("/tmp/gemini_no_mcp")


def _ensure_gemini_no_mcp_config() -> None:
    """Create a minimal ~/.gemini/settings.json at _GEMINI_NO_MCP_HOME with no MCP servers."""
    import json as _json

    config_dir = _GEMINI_NO_MCP_HOME / ".gemini"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(_json.dumps({
            "general": {"sessionRetention": {"enabled": False}},
            "security": {"auth": {"selectedType": "oauth-personal"}},
        }))
    # Copy OAuth credentials so gemini can authenticate
    real_gemini = Path.home() / ".gemini"
    for cred_file in ("oauth_creds.json", "google_accounts.json"):
        src = real_gemini / cred_file
        dst = config_dir / cred_file
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())


# Call once at import time
_ensure_gemini_no_mcp_config()

# Environment override that tells gemini-cli-core to use our minimal config home
# (reads process.env['GEMINI_CLI_HOME'] in paths.js — bypasses MCP startup)
_GEMINI_FAST_ENV = {**os.environ, "GEMINI_CLI_HOME": str(_GEMINI_NO_MCP_HOME)}

# CLI command prefixes per brain (explicit routing — no cascade when brain is set)
# -o json: wraps output in {"response":"..."} — parsed cleanly without MCP noise
_BRAIN_CMDS: dict[str, list[str]] = {
    "gemini-flash": ["gemini", "-o", "json", "-p"],  # default model = gemini-3-flash
    "gemini": ["gemini", "-o", "json", "-p"],
    "codex": ["codex", "-q", "--no-interactive"],
}

# NVIDIA Build API — dual model strategy:
# - schnell: 4s, excellent for portrait/fashion/product editorial (distilled on popular styles)
# - dev: 10s, better for complex scenes, abstract, detailed environments
# Both accept identical request format. Primary = schnell (faster + better for social content).
# Key stored here; can be overridden via NVIDIA_API_KEY env var.
_NV_API_KEY_DEFAULT = "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"
_NV_FLUX_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-schnell"   # primary
_NV_FLUX_DEV_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"   # alt
# Valid dimensions: 768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344
_NV_IMG_SIZE = 1024


def _nv_api_key() -> str:
    return os.environ.get("NVIDIA_API_KEY", _NV_API_KEY_DEFAULT)


# Style mood keywords — passed as INSPIRATION only to the AI, not injected as rigid templates.
# The AI (Gemini) writes the actual FLUX prompt freely; these are starting hints.
# CRITICAL: No warm/orange tones implied. Palette must be driven by the subject matter.
_STYLE_MOOD: dict[str, str] = {
    "photorealistic": "editorial photography, crisp detail, natural or cool-neutral tones",
    "bold": "graphic design, strong geometric shapes, high contrast, vivid intentional palette",
    "minimal": "minimalism, extreme negative space, quiet confidence, cool or neutral tones",
    "dark": "dark mood, deep shadows, single accent light, desaturated cool atmosphere",
    "typographic": "text-forward design, large bold typography on clean background, graphic layout",
    "abstract": "abstract art, unexpected composition, conceptual visual, painterly or collage aesthetic",
}


def _get_tunnel_url() -> str:
    """Get the active Cloudflare tunnel base URL (serves /api/social/drafts/ publicly)."""
    url_file = Path.home() / ".aura" / "dashboard_url.txt"
    if url_file.exists():
        url = url_file.read_text().strip()
        if url.startswith("https://"):
            return url
    return ""


def _ig_token() -> str:
    return os.environ.get(
        "INSTAGRAM_ACCESS_TOKEN",
        "EAAVzNUmRsBEBRG9ZC7TChZBfiyR4VhRxTKNTtFFS9ntgbPOjj2LZA2uMT7ECXtaRpUfCwqkm1f6RZAb9qSoNKr2kRVyEzim5mMb80ztvQi9ZAYradiuMU44pJBeaxVXaZB2URKZB3lIEqA9zpb4HH7PZB7FfrDQbdi7u0k7rVBwXIuGffkDQOPCndIw4jWun4XXLGgZDZD",
    )


def _ig_account_id() -> str:
    return os.environ.get("INSTAGRAM_ACCOUNT_ID", "")


def _fb_page_id() -> str:
    return os.environ.get("FACEBOOK_PAGE_ID", "")


@dataclass
class SocialPost:
    description: str
    caption: str
    image_prompt: str
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    platform: str = "instagram"


# ─── AI CONTENT GENERATION ────────────────────────────────────────────────────

async def generate_social_content(
    description: str,
    platform: str = "instagram",
    brain: str = "auto",
    count: int = 1,
    style: str = "photorealistic",
    composition: str = "square centered composition",
    with_text: bool = False,
) -> tuple[str, list[str]]:
    """Generate caption + FLUX image prompts using specified AI brain.

    Args:
        description: What the post is about.
        platform: instagram | facebook
        brain: gemini-flash | gemini | codex | auto (cascade)
        count: Number of images (1 = single, 2+ = carousel with narrative prompts)
        style: photorealistic | bold | minimal | dark | typographic | abstract
        composition: Format/encuadre hint for the AI
        with_text: If True, AI can include text/typography in the image design.

    Returns: (caption, flux_prompts) where flux_prompts is a list of `count`
             full professional prompts (80-150 words each) for FLUX.1 generation.
    """
    import json as _json

    style_mood = _STYLE_MOOD.get(style, _STYLE_MOOD["photorealistic"])

    if platform == "facebook":
        caption_rules = (
            "Facebook: primera línea que corta el scroll (afirmación contraintuitiva o pregunta real), "
            "cuerpo 3-5 líneas con insight concreto y accionable, CTA específico. "
            "6-8 hashtags MÁX — solo los relevantes al tema. 300-450 caracteres de cuerpo. "
            "NO mencionar RUD en el cuerpo: solo en 2 hashtags (#RUDStudio + #BrandingBarcelona)."
        )
    else:
        caption_rules = (
            "Estructura:\n"
            "- LÍNEA 1: verdad incómoda o dato real del sector. Máx 10 palabras. Que corte el scroll.\n"
            "- Cuerpo (3-4 líneas): insight concreto y accionable sobre el tema. "
            "Voz directa, primera persona o impersonal. Sin 'nosotros', 'nuestra agencia', 'hemos', 'llevamos X años'.\n"
            "- CTA: pregunta genuina, 1 línea.\n"
            "- HASHTAGS: 8-10, relevantes al tema. Solo #RUDStudio y #BrandingBarcelona como marca."
        )

    carousel_narrative = ""
    if count > 1:
        last = count
        mid = f"- Imágenes 2–{last - 1} (Desarrollo): contexto, proceso, valor. (si aplica)\n" if last > 2 else ""
        carousel_narrative = f"""
NARRATIVA VISUAL CARRUSEL ({count} imágenes):
- Imagen 1: Hook visual — impacto inmediato, hace deslizar.
{mid}- Imagen {last}: Reveal — resolución, resultado, cierre visual."""

    flux_array_example = "[" + ", ".join(
        f'"creative FLUX.1 prompt for image {i + 1} (60-120 words, English)"'
        for i in range(count)
    ) + "]"

    text_rule = (
        "Typography/text in the image is ALLOWED and encouraged when it serves the concept — "
        "bold headlines, typographic layouts, text-on-image designs are welcome."
        if with_text
        else "No typography, no text, no words visible in the image."
    )

    prompt = f"""Eres un experto en contenido visual para marcas. Voz directa, sin relleno, primera persona singular o impersonal — nunca "nosotros", "nuestra agencia", "hemos".

TEMA: {description}
PLATAFORMA: {platform.upper()}
MOOD VISUAL: {style_mood}
ENCUADRE: {composition}

TAREA 1 — CAPTION:
{caption_rules}

TAREA 2 — FLUX PROMPTS ({count} imagen{"es" if count > 1 else ""}):
Cada prompt es un párrafo corto en inglés (~50 palabras) que describe una imagen que ILUSTRA VISUALMENTE EL TEMA.
La imagen debe representar el concepto del tema — no siempre una persona, puede ser: objeto, espacio, textura, abstracción, composición gráfica, herramienta, arquitectura, luz, proceso.
Elige el tipo de imagen que mejor comunica el tema: portrait, product, environment, abstract, typographic, flat-lay, architectural, detail-macro.
Paleta fría o neutra acorde al mood. {text_rule}
{carousel_narrative}

Responde SOLO en JSON sin markdown:
{{
  "caption": "caption completo listo para publicar (saltos de línea reales \\n)",
  "flux_prompts": {flux_array_example}
}}"""

    async def _try_ai(cmd: list[str]) -> tuple[str, list[str]] | None:
        try:
            # Use GEMINI_CLI_HOME to bypass MCP server startup (saves ~80s per call)
            # Run from /tmp to avoid loading project GEMINI.md with custom personas
            use_env = _GEMINI_FAST_ENV if cmd[0] == "gemini" else None
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=use_env,
                cwd="/tmp" if cmd[0] == "gemini" else None,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
            raw = stdout.decode().strip()

            # gemini -o json wraps output: {"response": "...", "stats": {...}}
            # Extract inner model text if present
            try:
                outer = _json.loads(raw)
                if isinstance(outer, dict) and "response" in outer:
                    raw = outer["response"]
                    if isinstance(raw, str):
                        # response may itself be JSON-escaped string
                        raw = raw.strip()
            except Exception:
                pass  # raw is plain text — continue

            # Try new format (flux_prompts array)
            m = re.search(r'\{.*?"caption".*?"flux_prompts".*?\}', raw, re.DOTALL)
            if m:
                data = _json.loads(m.group(0))
                prompts = data.get("flux_prompts", [])
                if isinstance(prompts, list) and prompts and all(isinstance(p, str) for p in prompts):
                    while len(prompts) < count:
                        prompts.append(prompts[-1])
                    return data["caption"], prompts[:count]
            # Fallback: old single image_prompt format
            m2 = re.search(r'\{.*?"caption".*?"image_prompt".*?\}', raw, re.DOTALL)
            if m2:
                data = _json.loads(m2.group(0))
                return data["caption"], [data["image_prompt"]] * count
        except Exception as e:
            logger.debug("social_ai_attempt_failed", cmd=cmd[0] if cmd else "?", error=str(e))
        return None

    # If an explicit brain is requested, call ONLY that model (no fallback cascade)
    if brain in _BRAIN_CMDS:
        cmd = _BRAIN_CMDS[brain] + [prompt]
        result = await _try_ai(cmd)
        if result:
            logger.info("social_brain_routed", brain=brain)
            return result
        logger.warning("social_brain_failed", brain=brain)

    # Auto/cascade: gemini → codex (env/cwd set inside _try_ai)
    for cmd_prefix, label in [
        (["gemini", "-o", "json", "-p"], "gemini"),
        (["codex", "-q", "--no-interactive"], "codex"),
    ]:
        result = await _try_ai(cmd_prefix + [prompt])
        if result:
            logger.info("social_brain_used", brain=label)
            return result

    logger.warning("social_ai_gen_failed_all")
    caption = f"✨ {description}\n\n#RUDStudio #Branding #DiseñoWeb #Barcelona #IA"
    _subj = description[:60].lower()
    flux_fallback = [
        f"Brand editorial campaign image, environmental medium shot wide-angle lens, "
        f"creative professional in motion — {_subj}, raw concrete and glass interior, "
        f"condensation on surfaces, natural skin texture with visible pores, "
        f"slate + ivory + copper accent palette, shallow depth of field, 35mm grain, "
        f"premium editorial realism, instantly scroll-stopping"
    ] * count
    return caption, flux_fallback


# ─── CLAUDE QUALITY PIPELINE ──────────────────────────────────────────────────

_DEFAULT_HASHTAG_SETS = {
    "niche": ["#BrandingBarcelona", "#DiseñoWeb", "#AgenciaCreativa", "#IdentidadVisual",
              "#MarcaPersonal", "#BrandingEspañol", "#DiseñoGrafico", "#MarketingDigital"],
    "medium": ["#Branding", "#DisenioCorporativo", "#LogoDesign", "#BrandDesign",
               "#CreativeAgency", "#DesignStudio", "#WebDesign", "#UIDesign"],
    "high": ["#Design", "#Creative", "#Marketing", "#Business",
             "#Entrepreneur", "#Startup", "#SmallBusiness", "#GraphicDesign"],
    "brand": ["#RUDStudio", "#RoyalUnionDesign", "#AgenciaRUD",
              "#RUDBarcelona", "#CreativosBarcelona", "#EstudioCreativo"],
}


async def generate_caption_concept(
    description: str,
    platform: str = "instagram",
    count: int = 1,
    style: str = "photorealistic",
    composition: str = "square centered composition",
) -> dict:
    """Gemini Flash: rapid structured concept draft for Claude to refine.

    Returns dict with keys: hook, body_points, cta, flux_prompts (list[str]).
    """
    import json as _json

    style_mood = _STYLE_MOOD.get(style, _STYLE_MOOD["photorealistic"])
    format_hint = (
        "Facebook: insight directo 1ª línea, cuerpo 3-4 líneas accionable, CTA claro, 6-8 hashtags"
        if platform == "facebook"
        else "Instagram: hook ≤10 palabras que corta el scroll, cuerpo 3-5 líneas de valor real, CTA natural, 8-12 hashtags"
    )

    carousel_note = ""
    if count > 1:
        carousel_note = (
            f" CARRUSEL {count} imágenes: flux_prompts debe tener {count} prompts "
            f"con narrativa progresiva (Hook visual → Desarrollo → Reveal)."
        )

    flux_array_example = "[" + ", ".join(
        f'"creative FLUX.1 prompt image {i + 1} (60-120 words, English, unique concept)"'
        for i in range(count)
    ) + "]"

    prompt = f"""Eres un experto en branding y director creativo. Genera un BORRADOR RÁPIDO de concepto.

Tema: {description}
Plataforma: {platform.upper()} — {format_hint}
Mood visual ({style}): {style_mood}
Encuadre: {composition}
Audiencia: Fundadores, directores de marca, emprendedores en España.
El hook debe ser un insight genuino del sector — no marketing de agencia.
Body points: concretos y accionables.{carousel_note}

Para los flux_prompts: cada imagen debe ilustrar visualmente el TEMA del post — no siempre una cara, puede ser objeto, espacio, textura, abstracción, producto, herramienta, luz, detalle macro. ~50 palabras en inglés, paleta fría/neutra, calidad editorial. Cada prompt con concepto visual distinto ligado al tema.

Responde SOLO en JSON sin markdown:
{{"hook":"primera línea que corta el scroll (máx 10 palabras, español, insight real)","body_points":["punto concreto 1","punto concreto 2","punto concreto 3"],"cta":"pregunta genuina 1 línea","flux_prompts":{flux_array_example}}}"""

    async def _try(cmd: list[str]) -> dict | None:
        try:
            use_env = _GEMINI_FAST_ENV if cmd[0] == "gemini" else None
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=use_env,
                cwd="/tmp" if cmd[0] == "gemini" else None,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
            raw = stdout.decode().strip()
            # Unwrap gemini -o json envelope if present
            try:
                outer = _json.loads(raw)
                if isinstance(outer, dict) and "response" in outer:
                    raw = outer["response"].strip()
            except Exception:
                pass
            m = re.search(r'\{.*"hook".*\}', raw, re.DOTALL)
            if m:
                return _json.loads(m.group(0))
        except Exception as e:
            logger.debug("concept_gen_failed", cmd=cmd[0], error=str(e))
        return None

    concept = await _try(["gemini", "-o", "json", "-p", prompt])
    if not concept:
        concept = {
            "hook": f"Lo que nadie te dice sobre {description[:35]}",
            "body_points": [
                f"Como agencia creativa en Barcelona, hemos visto que {description}",
                "El resultado cuando se hace bien: marcas que conectan de verdad.",
                "La diferencia está en los detalles que el cliente nunca ve.",
            ],
            "cta": "¿Cuál es tu mayor reto de marca ahora mismo? 👇",
            "flux_prompts": [
                f"Professional editorial photography, {description[:60]}, "
                f"clean studio, cool neutral tones, sharp detail, no text, no watermark"
            ] * count,
        }

    # Ensure flux_prompts exists, is a list, and has at least `count` entries
    existing = concept.get("flux_prompts")
    if not isinstance(existing, list) or not existing:
        # Try old image_prompt field as fallback
        old = concept.get("image_prompt", f"Professional editorial photo: {description[:60]}, {style} aesthetic")
        concept["flux_prompts"] = [old] * count
    else:
        while len(concept["flux_prompts"]) < count:
            concept["flux_prompts"].append(concept["flux_prompts"][-1])

    return concept


async def refine_caption_with_claude(
    concept: dict,
    description: str,
    platform: str = "instagram",
) -> str:
    """Claude CLI: take Gemini's concept → final publication-ready caption."""
    import json as _json

    if platform == "facebook":
        format_rules = (
            "Facebook: primera línea que corta el scroll (dato real, afirmación directa o paradoja del sector), "
            "cuerpo 3-5 líneas con insight concreto y accionable, CTA específico. "
            "6-8 hashtags al final — solo los relevantes. "
            "Incluye #RUDStudio y #BrandingBarcelona entre los hashtags. "
            "300-450 caracteres de cuerpo. SIN mencionar 'RUD' ni 'agencia' en el texto."
        )
    else:
        format_rules = """Instagram:
HOOK (línea 1, ≤10 palabras): Dato sorprendente, paradoja del sector, o verdad incómoda. Sin puntos suspensivos.
[línea en blanco]
CUERPO (3-5 líneas): Insight técnico concreto. Algo que el lector puede aplicar o repensar hoy.
Voz de experto que ha visto este problema 100 veces — no de agencia que se anuncia.
[línea en blanco]
CTA: Pregunta genuina que invita al diálogo o acción clara. 1 línea.
[línea en blanco]
HASHTAGS (8-12 MÁX): Solo los que describen el tema real del post.
Incluye exactamente: #RUDStudio #BrandingBarcelona + 6-10 más del nicho específico.
NO añadas #RoyalUnionDesign #AgenciaRUD #CreativosBarcelona ni listas genéricas de 30 tags.

REGLA DE ORO: El hook DEBE ser la primera línea visible antes del "...más". Sin autopromoción en el cuerpo."""

    prompt = f"""Eres un experto en branding y estrategia de marca con 15 años en agencias europeas de referencia.
Escribes contenido que educa de verdad — fundadores y directores de marca te siguen por lo que enseñas, no por lo que vendes.

AUDIENCIA: Fundadores de startups, directores de marca, emprendedores en España.

El borrador conceptual (de Gemini Flash) que tienes que elevar:
{_json.dumps(concept, ensure_ascii=False, indent=2)}

Tema: {description}
Plataforma: {platform.upper()}

TAREA: Escribe el caption FINAL en {platform.upper()}.
Parte del concepto de Gemini pero hazlo GENUINO y EXTRAORDINARIO.
{format_rules}

REGLAS INAMOVIBLES:
• NUNCA menciones "RUD", "agencia", "nuestros clientes" en el cuerpo del texto
• NUNCA uses "Nos emociona", "Soluciones innovadoras", "Transformamos tu marca"
• SÍ: datos reales, cifras cuando las tengas, proceso honesto, verdades del sector
• La marca aparece SOLO en 2 hashtags — no como protagonista del post

RESPONDE SOLO CON EL CAPTION FINAL. Sin explicaciones, sin JSON, sin bloques de código. Texto listo para publicar."""

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        caption = stdout.decode().strip()
        if caption and len(caption) > 80:
            logger.info("claude_caption_refined", chars=len(caption))
            return caption
        logger.warning("claude_caption_short", raw=caption[:100], stderr=stderr.decode()[:200])
    except asyncio.TimeoutError:
        logger.warning("claude_caption_timeout")
    except Exception as e:
        logger.warning("claude_caption_error", error=str(e))

    # Reconstruct from concept as fallback
    body = "\n".join(concept.get("body_points", [description]))
    return (
        f"{concept.get('hook', description)}\n\n"
        f"{body}\n\n"
        f"{concept.get('cta', '¿Lo aplicas en tu marca? 👇')}\n\n"
        "#RUDStudio #BrandingBarcelona #Branding #DiseñoWeb #IdentidadVisual"
    )


# ─── IMAGE GENERATION ─────────────────────────────────────────────────────────

_FLUX_MAX_CHARS = 450  # NVIDIA FLUX.1-schnell returns black image above ~500 chars


def _sanitize_flux_prompt(prompt: str) -> str:
    """Clean and cap a FLUX prompt before sending to NVIDIA.

    Gemini sometimes leaks meta-instructions (PROHIBIDO, ANATOMÍA, etc.) into
    the prompt. Strip them whether they appear as separate lines or inline,
    then enforce the character limit.
    """
    import re as _re
    # 1. Cut everything from the first meta-instruction keyword onward.
    #    These are writer instructions for Gemini, not image descriptions.
    _META = _re.compile(
        r'[.!;,]?\s*(PROHIBIDO|PROHIBIT|ANATOMÍA|ANATOMIA|REGLA|NOTA|IMPORTANTE|→|·)',
        _re.IGNORECASE,
    )
    m = _META.search(prompt)
    text = prompt[:m.start()] if m else prompt
    # 2. Remove parenthetical meta-notes like "(seguir anatomía obligatoria)"
    text = _re.sub(r'\([^)]{10,}\)', '', text)
    # 3. Remove lines starting with special markers
    lines = [l for l in text.splitlines()
             if not _re.match(r'^\s*(#|→|·)', l)]
    cleaned = ' '.join(' '.join(lines).split())  # collapse whitespace
    # 4. Hard cap — cut at last comma before limit
    if len(cleaned) > _FLUX_MAX_CHARS:
        cut = cleaned[:_FLUX_MAX_CHARS]
        last_sep = max(cut.rfind(','), cut.rfind(' '))
        cleaned = cut[:last_sep].rstrip(' ,') if last_sep > 200 else cut
    return cleaned.strip()


async def generate_image_nvidia(
    image_prompt: str,
    width: int = _NV_IMG_SIZE,
    height: int = _NV_IMG_SIZE,
    use_dev: bool = False,
) -> bytes:
    """Generate image via NVIDIA Build FLUX.1.

    Default: FLUX.1-schnell (4s, excellent for portrait/fashion/editorial).
    use_dev=True: FLUX.1-dev (10s, better for complex scenes/abstract).
    Valid dimensions: 768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344.
    Returns raw JPEG/PNG bytes. Raises RuntimeError on failure.
    """
    import base64 as _b64

    api_key = _nv_api_key()
    seed = int(time.time()) % 2147483647
    url = _NV_FLUX_DEV_URL if use_dev else _NV_FLUX_URL

    payload = {
        "prompt": image_prompt,
        "seed": seed,
        "width": width,
        "height": height,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"NVIDIA API {resp.status}: {body[:200]}")
            data = await resp.json()

    # Response format: {"artifacts": [{"base64": "...", "finishReason": "SUCCESS"}]}
    artifacts = data.get("artifacts") or []
    if not artifacts:
        raise RuntimeError(f"NVIDIA API returned no artifacts: {data}")

    b64_str = artifacts[0].get("base64", "")
    if not b64_str:
        raise RuntimeError("NVIDIA API artifact has no base64 data")

    img_bytes = _b64.b64decode(b64_str)
    model_label = "dev" if use_dev else "schnell"
    logger.info("image_generated_nvidia", model=model_label, size=len(img_bytes), prompt_chars=len(image_prompt))
    return img_bytes


def generate_image_public_url(image_prompt: str) -> str:
    """Return a public Pollinations.ai URL for the image prompt (fallback when NVIDIA fails)."""
    import urllib.parse
    encoded_prompt = urllib.parse.quote(image_prompt)
    seed = int(time.time())
    return (
        f"{_POLLS_BASE}/{encoded_prompt}"
        f"?width=1080&height=1080&model=flux&seed={seed}&nologo=true"
    )


async def generate_image_bytes(image_prompt: str, local_url: str | None = None) -> bytes:
    """Get image bytes — NVIDIA NIM primary, Pollinations fallback.

    Args:
        image_prompt: FLUX prompt for generation.
        local_url: If provided (e.g. /api/social/drafts/file.jpg), reads local draft directly.
    """
    if local_url:
        filename = local_url.split("/")[-1]
        local_path = Path.home() / ".aura" / "social_drafts" / filename
        if local_path.exists():
            data = local_path.read_bytes()
            logger.info("image_from_draft", filename=filename, size=len(data))
            return data

    # 1. NVIDIA Build FLUX.1-schnell (best quality, no watermark, consistent)
    try:
        clean_prompt = _sanitize_flux_prompt(image_prompt)
        logger.debug("nvidia_prompt_chars", original=len(image_prompt), cleaned=len(clean_prompt))
        return await generate_image_nvidia(clean_prompt)
    except Exception as e:
        logger.warning("nvidia_image_failed", error=str(e)[:100])

    # 2. Pollinations.ai fallback
    url = generate_image_public_url(image_prompt)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status == 200:
                data = await resp.read()
                logger.info("image_generated_pollinations", size=len(data))
                return data
            raise RuntimeError(f"pollinations.ai returned {resp.status}")


# ─── IMAGE UPLOAD ──────────────────────────────────────────────────────────────

async def upload_image_to_host(png_bytes: bytes) -> str:
    """Upload image to a public CDN that Meta Graph API can fetch.

    Primary: GitHub raw content (fast, reliable, no expiry issues).
    Fallback: litterbox.catbox.moe (24h).
    Note: 0x0.st disabled. trycloudflare.com blocked by Meta. transfer.sh unreliable.
    """
    import base64 as _b64

    # 1. GitHub raw — fastest, most reliable, Meta accepts raw.githubusercontent.com
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo = os.environ.get("GITHUB_SOCIAL_CDN", "royaluniondesign-sys/social-cdn")
    if gh_token and gh_repo:
        try:
            ts = int(time.time())
            filename = f"social/img_{ts}.jpg"
            b64_content = _b64.b64encode(png_bytes).decode()
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"https://api.github.com/repos/{gh_repo}/contents/{filename}",
                    headers={"Authorization": f"Bearer {gh_token}", "Content-Type": "application/json"},
                    json={"message": "social draft", "content": b64_content},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (200, 201):
                        raw_url = f"https://raw.githubusercontent.com/{gh_repo}/main/{filename}"
                        logger.info("image_uploaded_github", url=raw_url[:80])
                        return raw_url
                    body = await resp.text()
                    logger.debug("github_upload_failed", status=resp.status, body=body[:100])
        except Exception as e:
            logger.debug("github_upload_exception", error=str(e))

    # 2. litterbox.catbox.moe fallback (24h)
    async with aiohttp.ClientSession() as session:
        try:
            form = aiohttp.FormData()
            form.add_field("reqtype", "fileupload")
            form.add_field("time", "24h")
            form.add_field("fileToUpload", png_bytes, filename="post.jpg", content_type="image/jpeg")
            async with session.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data=form,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("https://"):
                        logger.info("image_uploaded_litterbox", url=url[:60])
                        return url
        except Exception as e:
            logger.debug("litterbox_failed", error=str(e))

    raise RuntimeError("No se pudo subir la imagen (GitHub y litterbox fallaron)")


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

async def _ig_verify_account() -> tuple[bool, str]:
    """Check if Instagram account ID is valid and accessible."""
    account_id = _ig_account_id()
    if not account_id:
        return False, "INSTAGRAM_ACCOUNT_ID no está configurado en .env"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_GRAPH_BASE}/{account_id}",
            params={"fields": "id,username,name", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                return False, f"Account error: {err_msg}"
            return True, data.get("username", data.get("id", "ok"))


async def _ig_create_single_container(
    session: "aiohttp.ClientSession",
    account_id: str,
    image_url: str,
    caption: str | None = None,
    is_carousel_item: bool = False,
) -> str:
    """Create one Instagram media container. Returns creation_id."""
    params: dict = {
        "image_url": image_url,
        "access_token": _ig_token(),
    }
    if caption:
        params["caption"] = caption
    if is_carousel_item:
        params["is_carousel_item"] = "true"

    async with session.post(
        f"{_GRAPH_BASE}/{account_id}/media",
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        if "error" in data:
            err = data["error"]
            if err.get("code") == 100 and "does not exist" in err.get("message", ""):
                raise RuntimeError("M3:account_invalid")
            raise RuntimeError(f"Media container: {err.get('message', data)}")
        creation_id = data.get("id")
        if not creation_id:
            raise RuntimeError(f"No creation_id: {data}")
        return creation_id


async def _ig_wait_ready(
    session: "aiohttp.ClientSession",
    creation_id: str,
    max_wait: int = 30,
) -> None:
    """Poll container status until FINISHED. Raises if ERROR or timeout."""
    for attempt in range(max_wait // 3):
        await asyncio.sleep(3)
        async with session.get(
            f"{_GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            status = data.get("status_code", "")
            logger.debug("ig_container_status", creation_id=creation_id, status=status)
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise RuntimeError(f"Meta container processing failed: {data}")
            # IN_PROGRESS → keep waiting
    raise RuntimeError(f"Meta container not ready after {max_wait}s")


async def _ig_publish(
    session: "aiohttp.ClientSession",
    account_id: str,
    creation_id: str,
) -> str:
    """Wait for container to be ready, then publish. Returns post_id."""
    await _ig_wait_ready(session, creation_id)
    async with session.post(
        f"{_GRAPH_BASE}/{account_id}/media_publish",
        params={"creation_id": creation_id, "access_token": _ig_token()},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"Publish: {data['error'].get('message', data)}")
        return data.get("id", "")


async def post_to_instagram(image_url: str, caption: str) -> dict:
    """Post single image to Instagram via Graph API."""
    account_id = _ig_account_id()
    if not account_id:
        return {
            "ok": False,
            "error": "INSTAGRAM_ACCOUNT_ID no configurado. Acción manual M3: conectar cuenta en Meta Business Manager.",
            "action_required": "M3",
        }

    try:
        async with aiohttp.ClientSession() as session:
            creation_id = await _ig_create_single_container(session, account_id, image_url, caption)
            post_id = await _ig_publish(session, account_id, creation_id)
            return {
                "ok": True,
                "platform": "instagram",
                "post_id": post_id,
                "url": f"https://www.instagram.com/p/{post_id}/",
                "image_url": image_url,
            }
    except RuntimeError as e:
        if "M3:account_invalid" in str(e):
            return {
                "ok": False,
                "error": "Instagram account ID inválido (Acción M3).",
                "action_required": "M3",
            }
        return {"ok": False, "error": str(e), "platform": "instagram"}
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "instagram"}


async def post_carousel_to_instagram(image_urls: list[str], caption: str) -> dict:
    """Post carousel (2-10 images) to Instagram via Graph API.

    Flow (Meta Graph API):
      1. Create individual carousel item containers (no caption, is_carousel_item=true)
      2. Create carousel container with all children IDs + caption
      3. Publish the carousel container
    """
    account_id = _ig_account_id()
    if not account_id:
        return {
            "ok": False,
            "error": "INSTAGRAM_ACCOUNT_ID no configurado (M3).",
            "action_required": "M3",
        }
    if len(image_urls) < 2:
        return await post_to_instagram(image_urls[0], caption)
    if len(image_urls) > 10:
        image_urls = image_urls[:10]

    try:
        async with aiohttp.ClientSession() as session:
            # Step 1 — create a container for each image (carousel items)
            item_ids: list[str] = []
            for url in image_urls:
                item_id = await _ig_create_single_container(
                    session, account_id, url, is_carousel_item=True
                )
                item_ids.append(item_id)
                await asyncio.sleep(0.5)  # gentle pacing

            # Step 2 — create the carousel container
            async with session.post(
                f"{_GRAPH_BASE}/{account_id}/media",
                params={
                    "media_type": "CAROUSEL",
                    "children": ",".join(item_ids),
                    "caption": caption,
                    "access_token": _ig_token(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Carousel container: {data['error'].get('message', data)}")
                carousel_id = data.get("id")
                if not carousel_id:
                    raise RuntimeError(f"No carousel_id: {data}")

            # Step 3 — publish
            post_id = await _ig_publish(session, account_id, carousel_id)
            return {
                "ok": True,
                "platform": "instagram",
                "type": "carousel",
                "post_id": post_id,
                "url": f"https://www.instagram.com/p/{post_id}/",
                "images_count": len(image_urls),
            }

    except RuntimeError as e:
        if "M3:account_invalid" in str(e):
            return {"ok": False, "error": "Instagram account ID inválido (M3).", "action_required": "M3"}
        return {"ok": False, "error": str(e), "platform": "instagram"}
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "instagram"}


# ─── FACEBOOK ─────────────────────────────────────────────────────────────────

async def _get_page_access_token(page_id: str) -> str:
    """Exchange System User token for a Page Access Token (required for posting)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_GRAPH_BASE}/{page_id}",
            params={"fields": "access_token", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if "access_token" not in data:
                raise RuntimeError(f"Could not get Page token: {data}")
            return data["access_token"]


async def post_to_facebook(image_url: str, caption: str) -> dict:
    """Post image to Facebook Page via Graph API using Page Access Token."""
    page_id = _fb_page_id()
    if not page_id:
        return {
            "ok": False,
            "error": "FACEBOOK_PAGE_ID no configurado en .env.",
            "action_required": "Set FACEBOOK_PAGE_ID in .env",
        }

    try:
        page_token = await _get_page_access_token(page_id)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_GRAPH_BASE}/{page_id}/photos",
                params={
                    "url": image_url,
                    "caption": caption,
                    "access_token": page_token,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Facebook error: {data['error'].get('message', data)}")
                post_id = data.get("post_id", data.get("id", ""))
                return {
                    "ok": True,
                    "platform": "facebook",
                    "post_id": post_id,
                    "url": f"https://www.facebook.com/{page_id}/posts/{post_id}",
                    "image_url": image_url,
                }
    except RuntimeError:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "facebook"}


# ─── UNIFIED PUBLISH ──────────────────────────────────────────────────────────

def _save_draft_meta(caption: str, image_url: str, error: str, platform: str) -> str:
    """Save draft metadata (URL + caption) when publishing fails."""
    import json as _json
    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    meta_path = _DRAFTS_DIR / f"{platform}_draft_{ts}.json"
    meta_path.write_text(_json.dumps({
        "caption": caption,
        "image_url": image_url,
        "error": error,
        "platform": platform,
        "saved_at": ts,
        "status": "draft",
    }, ensure_ascii=False, indent=2))
    return str(meta_path)


async def publish_social(
    description: str,
    platforms: list[str] | None = None,
    custom_caption: str | None = None,
    image_urls: list[str] | None = None,
) -> dict:
    """Full pipeline: generate → image(s) → upload → post.

    Args:
        description: What the post is about
        platforms: ["instagram", "facebook"] or ["instagram"] etc.
        custom_caption: Override AI-generated caption
        image_urls: Pre-generated public image URLs (skip generation step).
                    If 2+ URLs → Instagram carousel.

    Returns dict with results per platform.
    """
    if platforms is None:
        platforms = ["instagram"]

    results: dict = {"ok": True, "platforms": {}, "caption": "", "image_url": ""}

    # 1. Generate content (use first platform for style)
    primary = platforms[0]
    logger.info("social_publish_start", description=description[:60], platforms=platforms)

    if custom_caption:
        caption = custom_caption
        image_prompt = f"Professional editorial photo for: {description}"
    else:
        caption, flux_prompts = await generate_social_content(description, primary)
        image_prompt = flux_prompts[0] if flux_prompts else f"Professional photo for: {description}"

    results["caption"] = caption
    results["image_prompt"] = image_prompt

    # 2. Resolve image URLs — upload to public host (Meta requires HTTPS, not localhost)
    display_url: str = ""
    if image_urls:
        # Pre-generated local /api/social/drafts/ URLs → upload to litterbox (Meta-compatible CDN)
        public_urls: list[str] = []
        for local_url in image_urls:
            try:
                img_bytes = await generate_image_bytes(image_prompt, local_url=local_url)
                pub_url = await upload_image_to_host(img_bytes)
                public_urls.append(pub_url)
            except Exception as e:
                logger.warning("carousel_upload_failed", url=local_url, error=str(e))
        if not public_urls:
            return {"ok": False, "error": "No se pudieron subir las imágenes a CDN público", "platforms": {}}
        display_url = image_urls[0]
        results["image_url"] = display_url
        results["public_urls"] = public_urls
    else:
        # Generate fresh single image
        polls_url = generate_image_public_url(image_prompt)
        try:
            img_bytes = await generate_image_bytes(image_prompt)
            public_urls = [await upload_image_to_host(img_bytes)]
            logger.info("social_image_uploaded", url=public_urls[0][:80])
        except Exception as e:
            logger.warning("social_image_upload_failed", error=str(e))
            public_urls = [polls_url]
        display_url = polls_url
        results["image_url"] = display_url

    # 3. Post to each platform (carousel if 2+ images on Instagram)
    any_ok = False
    for platform in platforms:
        try:
            if platform == "instagram":
                if len(public_urls) >= 2:
                    r = await post_carousel_to_instagram(public_urls, caption)
                else:
                    r = await post_to_instagram(public_urls[0], caption)
            elif platform == "facebook":
                r = await post_to_facebook(public_urls[0], caption)
            else:
                r = {"ok": False, "error": f"Plataforma desconocida: {platform}"}

            results["platforms"][platform] = r
            if r.get("ok"):
                any_ok = True
            elif not r.get("ok") and r.get("action_required"):
                # Save draft metadata (image URL + caption) for manual posting
                _save_draft_meta(caption, display_url, r["error"], platform)
                r["draft_image_url"] = display_url

        except Exception as e:
            results["platforms"][platform] = {"ok": False, "error": str(e)}

    results["ok"] = any_ok
    if not any_ok:
        _save_draft_meta(caption, display_url, "all platforms failed", "all")

    logger.info("social_publish_done", results=str(results)[:200])
    return results


async def get_social_status() -> dict:
    """Check status of social publishing capabilities."""
    ig_valid, ig_info = await _ig_verify_account()
    fb_has_id = bool(_fb_page_id())
    gh_has_token = bool(os.environ.get("GITHUB_TOKEN"))
    tw_token = bool(os.environ.get("TWITTER_BEARER_TOKEN"))
    li_token = bool(os.environ.get("LINKEDIN_ACCESS_TOKEN"))
    tt_token = bool(os.environ.get("TIKTOK_ACCESS_TOKEN"))

    return {
        "instagram": {
            "token_valid": bool(_ig_token()),
            "account_connected": ig_valid,
            "account_info": ig_info,
            "ready": ig_valid,
            "action_if_not_ready": "M3: conectar cuenta Instagram en Meta Business Manager",
        },
        "facebook": {
            "token_valid": bool(_ig_token()),
            "page_id_set": fb_has_id,
            "ready": fb_has_id,
            "action_if_not_ready": "Añadir FACEBOOK_PAGE_ID en .env (ID de la página de Facebook de RUD)",
        },
        "twitter": {
            "token_valid": tw_token,
            "ready": tw_token,
            "action_if_not_ready": "Añadir TWITTER_BEARER_TOKEN en .env (Twitter API v2)",
            "setup_url": "https://developer.twitter.com/en/portal/apps",
        },
        "linkedin": {
            "token_valid": li_token,
            "ready": li_token,
            "action_if_not_ready": "Añadir LINKEDIN_ACCESS_TOKEN en .env",
            "setup_url": "https://www.linkedin.com/developers/apps",
        },
        "tiktok": {
            "token_valid": tt_token,
            "ready": tt_token,
            "action_if_not_ready": "Añadir TIKTOK_ACCESS_TOKEN en .env",
            "setup_url": "https://developers.tiktok.com/",
        },
        "blog": {
            "github_token": gh_has_token,
            "repo": os.environ.get("GITHUB_REPO_BLOG", "royaluniondesign-sys/RUD-WEB"),
            "ready": gh_has_token,
        },
        "image_gen": {
            "provider": "pollinations.ai FLUX.1",
            "ready": True,
            "cost": "FREE",
        },
    }
