"""Image generation backends for social posts — NVIDIA, BFL, ComfyUI, Pollinations."""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import aiohttp
import structlog

logger = structlog.get_logger()

_POLLS_BASE = "https://image.pollinations.ai/prompt"
_FLUX_MAX_CHARS = 500

# Literal SD/LoRA/meta syntax that breaks FLUX
_SD_GARBAGE = re.compile(
    r'\b(LORA|LOCON|EMBEDDING|HYPERNETWORK|<[^>]+>|__[^_]+__)\b'
    r'|[.!;,]?\s*(PROHIBIDO|PROHIBIT|ANATOMÍA|ANATOMIA|REGLA|NOTA|IMPORTANTE)'
    r'|(?:^|\n)\s*[#→·].*',
    re.IGNORECASE | re.MULTILINE,
)
# Actual 3D/render artifacts — not style words, literally wrong medium
_RENDER_ARTIFACTS = re.compile(
    r'\b(3[dD] render|CGI render|photorealistic render|hyperrealistic render'
    r'|text overlay|glowing overlay|promotional poster)\b',
    re.IGNORECASE,
)

# NVIDIA Build API — dual model strategy:
# - dev: better quality, cinematic detail, strong prompt following (primary)
# - schnell: 4s, faster (fallback)
_NV_API_KEY_DEFAULT = "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"
_NV_FLUX_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
_NV_FLUX_SCHNELL_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-schnell"
_NV_IMG_SIZE = 1024

_BFL_SUBMIT_URL = "https://api.bfl.ai/v1/flux-pro-1.1-ultra"
_BFL_POLL_URL = "https://api.bfl.ai/v1/get_result"

_COMFYUI_URL = "http://127.0.0.1:8188"
# Aspect ratio → (width, height) for ComfyUI FLUX — must be multiples of 16
_COMFY_DIMS: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "4:5":  (1024, 1280),
    "9:16": (768,  1344),
    "16:9": (1344, 768),
}


def _nv_api_key() -> str:
    return os.environ.get("NVIDIA_API_KEY", _NV_API_KEY_DEFAULT)


def _sanitize_flux_prompt(prompt: str) -> str:
    """Remove technical garbage from a prompt without imposing any style.

    Only strips SD/LoRA syntax, meta-instruction leakage, literal 3D-render
    artifacts. Does NOT enforce film style, quality suffixes, or strip
    legitimate descriptive words.
    """
    m = re.search(r'[.!;,]?\s*(PROHIBIDO|PROHIBIT|ANATOMÍA|ANATOMIA|REGLA|NOTA|IMPORTANTE|→|·)', prompt, re.IGNORECASE)
    text = prompt[:m.start()] if m else prompt
    text = re.sub(r'\([^)]{20,}\)', '', text)
    text = _SD_GARBAGE.sub(' ', text)
    text = _RENDER_ARTIFACTS.sub('', text)
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'(,\s*){2,}', ', ', text)
    text = text.strip().strip(',').strip()
    if len(text) > _FLUX_MAX_CHARS:
        cut = text[:_FLUX_MAX_CHARS]
        last_sep = max(cut.rfind(','), cut.rfind(' '))
        text = cut[:last_sep].rstrip(' ,') if last_sep > 200 else cut
    return text


async def generate_image_nvidia(
    image_prompt: str,
    width: int = _NV_IMG_SIZE,
    height: int = _NV_IMG_SIZE,
    use_dev: bool = True,
) -> bytes:
    """Generate image via NVIDIA Build FLUX.1-dev.

    Valid dimensions: 768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344.
    Returns raw JPEG/PNG bytes. Raises RuntimeError on failure.
    """
    import base64 as _b64

    api_key = _nv_api_key()
    seed = int(time.time()) % 2147483647
    payload = {
        "prompt": image_prompt,
        "seed": seed,
        "width": width,
        "height": height,
        "steps": 30,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _NV_FLUX_URL,
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

    artifacts = data.get("artifacts") or []
    if not artifacts:
        raise RuntimeError(f"NVIDIA API returned no artifacts: {data}")

    b64_str = artifacts[0].get("base64", "")
    if not b64_str:
        raise RuntimeError("NVIDIA API artifact has no base64 data")

    img_bytes = _b64.b64decode(b64_str)
    logger.info("image_generated_nvidia", model="flux.1-dev", size=len(img_bytes), prompt_chars=len(image_prompt))
    return img_bytes


async def generate_image_bfl(
    image_prompt: str,
    aspect_ratio: str = "1:1",
    image_prompt_b64: str | None = None,
    image_prompt_strength: float = 0.15,
) -> bytes:
    """Generate image via BFL FLUX 1.1 Pro Ultra with raw mode.

    Raises RuntimeError on failure.
    """
    api_key = os.environ.get("BFL_API_KEY", "")
    if not api_key:
        raise RuntimeError("BFL_API_KEY not set")

    headers = {"x-key": api_key, "Content-Type": "application/json"}
    payload: dict = {
        "prompt": image_prompt,
        "aspect_ratio": aspect_ratio,
        "raw": True,
        "prompt_upsampling": True,
        "output_format": "jpeg",
        "safety_tolerance": 6,
    }
    if image_prompt_b64:
        payload["image_prompt"] = image_prompt_b64
        payload["image_prompt_strength"] = image_prompt_strength

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _BFL_SUBMIT_URL,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"BFL submit {resp.status}: {body[:200]}")
            submit_data = await resp.json()

        task_id = submit_data.get("id")
        if not task_id:
            raise RuntimeError(f"BFL no task id: {submit_data}")

        for _ in range(60):
            await asyncio.sleep(1.5)
            async with session.get(
                _BFL_POLL_URL,
                headers=headers,
                params={"id": task_id},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as poll:
                result = await poll.json()
            status = result.get("status")
            if status == "Ready":
                img_url = result["result"]["sample"]
                async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=30)) as dl:
                    img_bytes = await dl.read()
                logger.info("image_generated_bfl", kb=len(img_bytes) // 1024, prompt_chars=len(image_prompt))
                return img_bytes
            if status in ("Error", "Failed", "Content Moderated", "Request Moderated"):
                raise RuntimeError(f"BFL task {status}: {result}")

        raise RuntimeError("BFL timeout after 90s polling")


async def generate_image_comfyui(
    image_prompt: str,
    aspect_ratio: str = "1:1",
    steps: int = 25,
    seed: int | None = None,
) -> bytes:
    """Generate image via local ComfyUI + FLUX.1-dev GGUF (runs on Apple MPS).

    ComfyUI must be running on localhost:8188.
    Raises RuntimeError if ComfyUI is unreachable or generation fails.
    """
    import json as _json
    import uuid as _uuid

    width, height = _COMFY_DIMS.get(aspect_ratio, (1024, 1024))
    if seed is None:
        seed = int(time.time()) % 2147483647

    workflow_path = Path(__file__).parent.parent.parent / "comfyui_flux_workflow.json"
    if not workflow_path.exists():
        raise RuntimeError(f"ComfyUI workflow not found: {workflow_path}")
    workflow: dict = _json.loads(workflow_path.read_text())

    workflow["4"]["inputs"]["text"] = image_prompt
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["6"]["inputs"]["steps"] = steps
    workflow["6"]["inputs"]["seed"] = seed

    client_id = str(_uuid.uuid4())

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_COMFYUI_URL}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"ComfyUI submit {resp.status}: {body[:200]}")
            data = await resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI no prompt_id: {data}")

        for _ in range(400):
            await asyncio.sleep(1.5)
            async with session.get(
                f"{_COMFYUI_URL}/history/{prompt_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as poll:
                history = await poll.json()
            if prompt_id not in history:
                continue
            outputs = history[prompt_id].get("outputs", {})
            for node_output in outputs.values():
                images = node_output.get("images", [])
                if images:
                    img_info = images[0]
                    async with session.get(
                        f"{_COMFYUI_URL}/view",
                        params={
                            "filename": img_info["filename"],
                            "subfolder": img_info.get("subfolder", ""),
                            "type": img_info.get("type", "output"),
                        },
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as dl:
                        img_bytes = await dl.read()
                    logger.info(
                        "image_generated_comfyui",
                        kb=len(img_bytes) // 1024,
                        steps=steps,
                        res=f"{width}x{height}",
                    )
                    return img_bytes

        raise RuntimeError("ComfyUI timeout after 120s")


def generate_image_public_url(image_prompt: str) -> str:
    """Return a public Pollinations.ai URL (fallback when NVIDIA fails)."""
    import urllib.parse
    encoded_prompt = urllib.parse.quote(image_prompt)
    seed = int(time.time())
    return (
        f"{_POLLS_BASE}/{encoded_prompt}"
        f"?width=1080&height=1080&model=flux&seed={seed}&nologo=true"
    )


def _is_comfyui_running() -> bool:
    """Quick check if local ComfyUI is reachable on port 8188."""
    import urllib.request as _urllib
    try:
        _urllib.urlopen(f"{_COMFYUI_URL}/system_stats", timeout=2)
        return True
    except Exception:
        return False


async def generate_image_bytes(image_prompt: str, local_url: str | None = None) -> bytes:
    """Get image bytes — ComfyUI local (primary when running), NVIDIA NIM, Pollinations fallback.

    Priority:
      1. Local ComfyUI + Flux1-Dev GGUF — highest quality, ~60-120s, no rate limits
      2. NVIDIA Build API Flux.1-dev — cloud, fast, good quality
      3. Pollinations.ai — last resort fallback
    """
    if local_url:
        filename = local_url.split("/")[-1]
        local_path = Path.home() / ".aura" / "social_drafts" / filename
        if local_path.exists():
            data = local_path.read_bytes()
            logger.info("image_from_draft", filename=filename, size=len(data))
            return data

    if _is_comfyui_running():
        try:
            clean_prompt = _sanitize_flux_prompt(image_prompt)
            logger.info("comfyui_primary", prompt_chars=len(clean_prompt))
            return await generate_image_comfyui(clean_prompt)
        except Exception as e:
            logger.warning("comfyui_image_failed", error=str(e)[:200])

    try:
        clean_prompt = _sanitize_flux_prompt(image_prompt)
        logger.debug("nvidia_prompt_chars", original=len(image_prompt), cleaned=len(clean_prompt))
        return await generate_image_nvidia(clean_prompt)
    except Exception as e:
        logger.warning("nvidia_image_failed", error=str(e)[:100])

    url = generate_image_public_url(image_prompt)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status == 200:
                data = await resp.read()
                logger.info("image_generated_pollinations", size=len(data))
                return data
            raise RuntimeError(f"pollinations.ai returned {resp.status}")
