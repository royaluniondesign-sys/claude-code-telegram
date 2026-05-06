"""ComfyUI MCP tools for AURA — generate images via local Flux1-Dev.

Auto-discovered by AURA MCP server.
ComfyUI runs at localhost:8188 with Flux1-Dev Q4_K_S (GGUF).

Presets: social_post, story, banner, mockup, archviz, hyperreal, branding
"""

from __future__ import annotations

from src.actions.registry import aura_tool
from src.integrations import comfyui_client as comfy


@aura_tool(
    name="comfyui_generate",
    description=(
        "Generate an image using ComfyUI + Flux1-Dev. "
        "Describe what you want in natural language. "
        "Preset options: social_post, story, banner, mockup, archviz, hyperreal, branding."
    ),
    category="image",
    parameters={
        "prompt": {"type": "str", "description": "Detailed description of the image to generate"},
        "preset": {
            "type": "str",
            "description": "Optional preset: social_post | story | banner | mockup | archviz | hyperreal | branding",
        },
        "width": {"type": "int", "description": "Width in pixels (default 1024)"},
        "height": {"type": "int", "description": "Height in pixels (default 1024)"},
        "steps": {"type": "int", "description": "Sampling steps 10-50 (default 20). More = better quality, slower."},
    },
)
async def comfyui_generate(
    prompt: str,
    preset: str = "",
    width: int = 0,
    height: int = 0,
    steps: int = 0,
) -> str:
    if not comfy.is_running():
        return "❌ ComfyUI no está corriendo. Inicia com.aura.comfyui en launchctl."

    extra: dict = {}
    if width > 0:
        extra["width"] = width
    if height > 0:
        extra["height"] = height
    if steps > 0:
        extra["steps"] = steps

    try:
        result = await comfy.generate(
            prompt=prompt,
            preset=preset or None,
            extra_params=extra or None,
        )
        images = result["images"]
        if not images:
            return "❌ ComfyUI no devolvió imágenes."

        paths = "\n".join(f"  • {p}" for p in images)
        return (
            f"✅ Imagen generada ({result['estimated_time']})\n"
            f"Preset: {result['preset'] or 'ninguno'}\n"
            f"Prompt: {result['prompt_used'][:100]}...\n"
            f"Archivos:\n{paths}"
        )
    except TimeoutError:
        return "⏱️ Timeout — ComfyUI tardó demasiado. Revisa la cola en localhost:8188"
    except Exception as e:
        return f"❌ Error generando imagen: {e}"


@aura_tool(
    name="comfyui_status",
    description="Verifica estado de ComfyUI: si está corriendo, cola de generación, stats del sistema.",
    category="image",
    parameters={},
)
async def comfyui_status() -> str:
    if not comfy.is_running():
        return "❌ ComfyUI offline (puerto 8188 no responde)"

    try:
        stats = comfy.get_system_stats()
        queue = comfy.get_queue_status()
        ram_free = stats["system"]["ram_free"] // (1024**3)
        ram_total = stats["system"]["ram_total"] // (1024**3)
        version = stats["system"]["comfyui_version"]
        running = len(queue.get("queue_running", []))
        pending = len(queue.get("queue_pending", []))

        return (
            f"✅ ComfyUI {version} — Online\n"
            f"RAM: {ram_total - ram_free}/{ram_total} GB usada\n"
            f"Cola: {running} corriendo, {pending} pendientes\n"
            f"Modelos: Flux1-Dev Q4_K_S + CLIP-L + T5XXL + VAE\n"
            f"URL: http://localhost:8188"
        )
    except Exception as e:
        return f"❌ Error leyendo estado: {e}"


@aura_tool(
    name="comfyui_estimate",
    description="Estima cuánto tardará generar una imagen con los parámetros dados.",
    category="image",
    parameters={
        "steps": {"type": "int", "description": "Número de steps (default 20)"},
        "width": {"type": "int", "description": "Ancho en pixels (default 1024)"},
        "height": {"type": "int", "description": "Alto en pixels (default 1024)"},
    },
)
async def comfyui_estimate(steps: int = 20, width: int = 1024, height: int = 1024) -> str:
    est = comfy.estimate_time(steps, width, height)
    pixels = width * height
    quality = "rápida" if steps <= 20 else "calidad" if steps <= 30 else "alta calidad"
    return (
        f"⏱️ Estimación para {width}×{height}px, {steps} steps ({quality}):\n"
        f"**{est}** en Apple Silicon (Flux Q4_K_S)\n\n"
        f"Referencia:\n"
        f"• 1024×1024 / 20 steps → ~50s\n"
        f"• 1024×1024 / 30 steps → ~75s\n"
        f"• 1536×1024 / 25 steps → ~90s"
    )


@aura_tool(
    name="comfyui_presets",
    description="Lista todos los presets disponibles con su descripción y dimensiones.",
    category="image",
    parameters={},
)
async def comfyui_presets() -> str:
    presets = {
        "social_post": "1080×1080px — Posts Instagram/LinkedIn, fotografía de producto",
        "story":       "1080×1920px — Stories y Reels, formato vertical",
        "banner":      "1536×512px  — Banners web, cabeceras LinkedIn",
        "mockup":      "1024×1024px — Product mockups, packaging, branding aplicado",
        "archviz":     "1536×1024px — Renders arquitectónicos, interiores, exteriores",
        "hyperreal":   "1024×1024px — Fotografía hiperrealista, portrait, producto",
        "branding":    "1024×1024px — Identidad visual, logos concept, marca",
    }
    lines = ["🎨 **Presets ComfyUI disponibles:**\n"]
    for name, desc in presets.items():
        lines.append(f"• `{name}` — {desc}")
    lines.append("\nUso: comfyui_generate(prompt='...', preset='social_post')")
    return "\n".join(lines)
