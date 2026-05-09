"""AI content generation for social posts — captions, image prompts, concepts."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import structlog

logger = structlog.get_logger()

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
    real_gemini = Path.home() / ".gemini"
    for cred_file in ("oauth_creds.json", "google_accounts.json"):
        src = real_gemini / cred_file
        dst = config_dir / cred_file
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())


_ensure_gemini_no_mcp_config()

# Environment override that tells gemini-cli-core to use our minimal config home
_GEMINI_FAST_ENV = {**os.environ, "GEMINI_CLI_HOME": str(_GEMINI_NO_MCP_HOME)}

# CLI command prefixes per brain
_BRAIN_CMDS: dict[str, list[str]] = {
    "gemini-flash": ["gemini", "-o", "json", "-p"],
    "gemini": ["gemini", "-o", "json", "-p"],
    "codex": ["codex", "-q", "--no-interactive"],
}

# Style directives — each style defines a VISUAL TYPE + subject rule.
# These are HARD rules for the image generator, not vague mood hints.
_STYLE_DIRECTIVES: dict[str, dict[str, str]] = {
    "photorealistic": {
        "visual_type": "editorial product or environment photograph",
        "subject_rule": (
            "Photograph a physical object, material surface, workspace, tool, or environment "
            "that DIRECTLY represents the topic. A specific relevant thing — NOT a person's face. "
            "Could be: a brand manual open on a desk, a screen showing design work, packaging, "
            "a detail of a material, an architectural space, a product in context."
        ),
        "aesthetic": "shot on 35mm film, Kodak Portra 400, natural grain, soft ambient light, analog imperfection, matte finish, shallow depth of field",
    },
    "bold": {
        "visual_type": "graphic design poster — flat illustration, bold geometry",
        "subject_rule": (
            "Bold graphic composition with geometric shapes and flat design that SYMBOLIZES the topic. "
            "Abstract and conceptual — NO people, NO portraits, NO faces. "
            "Think: Bauhaus poster, Swiss design, bold flat shapes that convey the concept graphically."
        ),
        "aesthetic": "Bauhaus-inspired, bold flat shapes, high contrast palette, graphic poster energy, vector-clean",
    },
    "minimal": {
        "visual_type": "minimalist still life — single object, extreme negative space",
        "subject_rule": (
            "ONE single object or element that symbolizes the topic, centered on an almost empty "
            "background. The object must directly connect to the subject matter. "
            "NO people, NO clutter, NO multiple objects. Pure negative space around it."
        ),
        "aesthetic": "extreme minimalism, white or off-white background, single subject, clean quiet confidence",
    },
    "dark": {
        "visual_type": "cinematic noir still life or atmospheric environment",
        "subject_rule": (
            "A dark atmospheric scene — objects, surfaces, or environment that evoke the topic's "
            "core idea. Dramatic single-source lighting. "
            "NO portraits, NO people. Could be: an object in dramatic shadow, a dark workspace, "
            "an abstract surface texture, a product under spotlight in darkness."
        ),
        "aesthetic": "noir atmosphere, deep blacks, single accent light source, desaturated cool tones, cinematic 35mm grain",
    },
    "typographic": {
        "visual_type": "typography-first graphic design — text IS the visual",
        "subject_rule": (
            "Large bold typography conveying the KEY CONCEPT of the topic on a clean or textured background. "
            "The words must directly state or evoke the topic. Text is the hero — no people, no faces. "
            "Abstract geometric shapes can support the text as secondary elements."
        ),
        "aesthetic": "oversized display type, poster design, clean grid layout, strong typographic hierarchy",
    },
    "abstract": {
        "visual_type": "abstract conceptual art — shapes, color, texture as metaphor",
        "subject_rule": (
            "Pure abstract visual — NOT literal, NOT a face, NOT a recognizable person. "
            "Use flowing shapes, color fields, textures, layers, and abstract forms to convey the "
            "FEELING and CONCEPT of the topic. Like a museum-quality abstract painting or digital art."
        ),
        "aesthetic": "contemporary abstract art, painterly or collage technique, unexpected composition, conceptual depth",
    },
}

_STYLE_MOOD: dict[str, str] = {k: v["aesthetic"] for k, v in _STYLE_DIRECTIVES.items()}

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

    Returns: (caption, flux_prompts) where flux_prompts is a list of `count` prompts.
    """
    import json as _json

    style_dir = _STYLE_DIRECTIVES.get(style, _STYLE_DIRECTIVES["photorealistic"])
    style_visual_type = style_dir["visual_type"]
    style_subject_rule = style_dir["subject_rule"]
    style_aesthetic = style_dir["aesthetic"]

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
ENCUADRE: {composition}

TAREA 1 — CAPTION:
{caption_rules}

TAREA 2 — FLUX PROMPTS ({count} imagen{"es" if count > 1 else ""}):

TIPO DE IMAGEN OBLIGATORIO: {style_visual_type}

REGLA DEL SUJETO (seguir al pie de la letra):
{style_subject_rule}

ESTÉTICA: {style_aesthetic}

INSTRUCCIONES para el prompt de imagen:
1. El sujeto/escena debe ilustrar DIRECTAMENTE el TEMA: "{description}"
2. Seguir el TIPO DE IMAGEN indicado arriba — NO cambiar a retrato si no lo dice
3. ~60 palabras en inglés, técnicos y descriptivos
4. Paleta fría o neutra. {text_rule}
{carousel_narrative}

Responde SOLO en JSON sin markdown:
{{
  "caption": "caption completo listo para publicar (saltos de línea reales \\n)",
  "flux_prompts": {flux_array_example}
}}"""

    async def _try_ai(cmd: list[str]) -> tuple[str, list[str]] | None:
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

            try:
                outer = _json.loads(raw)
                if isinstance(outer, dict) and "response" in outer:
                    raw = outer["response"]
                    if isinstance(raw, str):
                        raw = raw.strip()
            except Exception:
                pass

            m = re.search(r'\{.*?"caption".*?"flux_prompts".*?\}', raw, re.DOTALL)
            if m:
                data = _json.loads(m.group(0))
                prompts = data.get("flux_prompts", [])
                if isinstance(prompts, list) and prompts and all(isinstance(p, str) for p in prompts):
                    while len(prompts) < count:
                        prompts.append(prompts[-1])
                    return data["caption"], prompts[:count]
            m2 = re.search(r'\{.*?"caption".*?"image_prompt".*?\}', raw, re.DOTALL)
            if m2:
                data = _json.loads(m2.group(0))
                return data["caption"], [data["image_prompt"]] * count
        except Exception as e:
            logger.debug("social_ai_attempt_failed", cmd=cmd[0] if cmd else "?", error=str(e))
        return None

    if brain in _BRAIN_CMDS:
        cmd = _BRAIN_CMDS[brain] + [prompt]
        result = await _try_ai(cmd)
        if result:
            logger.info("social_brain_routed", brain=brain)
            return result
        logger.warning("social_brain_failed", brain=brain)

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

    style_dir = _STYLE_DIRECTIVES.get(style, _STYLE_DIRECTIVES["photorealistic"])
    style_visual_type = style_dir["visual_type"]
    style_subject_rule = style_dir["subject_rule"]
    style_aesthetic = style_dir["aesthetic"]
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
        f'"FLUX.1 prompt image {i + 1} (60 words English, follows visual type below)"'
        for i in range(count)
    ) + "]"

    prompt = f"""Eres un experto en branding y director creativo. Genera un BORRADOR RÁPIDO de concepto.

Tema: {description}
Plataforma: {platform.upper()} — {format_hint}
Encuadre: {composition}
Audiencia: Fundadores, directores de marca, emprendedores en España.
El hook debe ser un insight genuino del sector — no marketing de agencia.
Body points: concretos y accionables.{carousel_note}

TIPO DE IMAGEN: {style_visual_type}
REGLA DEL SUJETO: {style_subject_rule}
ESTÉTICA: {style_aesthetic}

Para flux_prompts: seguir OBLIGATORIAMENTE el TIPO DE IMAGEN y REGLA DEL SUJETO.
El sujeto debe ilustrar directamente el TEMA. ~60 palabras en inglés, paleta fría/neutra.
Cada prompt distinto, todos conectados al tema, ninguno es un retrato genérico.

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

    existing = concept.get("flux_prompts")
    if not isinstance(existing, list) or not existing:
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

    body = "\n".join(concept.get("body_points", [description]))
    return (
        f"{concept.get('hook', description)}\n\n"
        f"{body}\n\n"
        f"{concept.get('cta', '¿Lo aplicas en tu marca? 👇')}\n\n"
        "#RUDStudio #BrandingBarcelona #Branding #DiseñoWeb #IdentidadVisual"
    )
