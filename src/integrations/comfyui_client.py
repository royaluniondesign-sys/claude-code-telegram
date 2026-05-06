"""ComfyUI integration client for AURA.

Connects to local ComfyUI instance (port 8188).
Supports: txt2img, presets, async generation with polling.
Models installed: Flux1-Dev GGUF Q4_K_S + CLIP-L + T5XXL + VAE
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

COMFYUI_HOST = os.getenv("COMFYUI_HOST", "127.0.0.1")
COMFYUI_PORT = int(os.getenv("COMFYUI_PORT", "8188"))
COMFYUI_BASE = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"
SKILLS_DIR = Path(os.getenv("COMFYUI_SKILLS_DIR", Path.home() / ".aura/comfyui/skills"))
OUTPUT_DIR = Path(os.getenv("COMFYUI_OUTPUT_DIR", Path.home() / ".aura/comfyui/outputs"))

# ComfyUI's own output dir
COMFYUI_OUTPUT_DIR = Path(os.getenv("COMFYUI_INSTALL_DIR",
    "/Users/oxyzen/Projects/ComfyUI")) / "output"


def is_running() -> bool:
    try:
        urllib.request.urlopen(f"{COMFYUI_BASE}/system_stats", timeout=3)
        return True
    except Exception:
        return False


def get_system_stats() -> dict:
    with urllib.request.urlopen(f"{COMFYUI_BASE}/system_stats", timeout=5) as r:
        return json.loads(r.read())


def get_queue_status() -> dict:
    with urllib.request.urlopen(f"{COMFYUI_BASE}/queue", timeout=5) as r:
        return json.loads(r.read())


def _load_skill(skill_name: str) -> tuple[dict, dict]:
    """Load workflow.json and schema.json for a skill."""
    skill_dir = SKILLS_DIR / skill_name
    workflow = json.loads((skill_dir / "workflow.json").read_text())
    schema = json.loads((skill_dir / "schema.json").read_text())
    return workflow, schema


def _apply_params(workflow: dict, schema: dict, params: dict) -> dict:
    """Apply user parameters to workflow nodes."""
    import copy
    wf = copy.deepcopy(workflow)

    # Apply schema defaults first
    for param_name, param_def in schema["parameters"].items():
        value = params.get(param_name, param_def.get("default"))
        if value is None:
            continue
        # Handle random seed
        if param_name == "seed" and value == -1:
            value = random.randint(0, 2**31)
        node_id = param_def["node_id"]
        field = param_def["field"]
        if node_id in wf and "inputs" in wf[node_id]:
            wf[node_id]["inputs"][field] = value

    return wf


def _apply_preset(schema: dict, preset_name: str | None, prompt: str) -> dict:
    """Build params from a preset, appending prompt suffix."""
    if not preset_name or preset_name not in schema.get("presets", {}):
        return {"prompt": prompt}

    preset = schema["presets"][preset_name]
    params = {k: v for k, v in preset.items() if k != "prompt_suffix"}
    suffix = preset.get("prompt_suffix", "")
    params["prompt"] = f"{prompt}{suffix}"
    return params


def submit_workflow(workflow: dict) -> str:
    """Submit workflow to ComfyUI, return prompt_id."""
    client_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFYUI_BASE}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise RuntimeError(f"ComfyUI rejected workflow: {data['error']}")
    return data["prompt_id"]


def get_history(prompt_id: str) -> dict:
    with urllib.request.urlopen(f"{COMFYUI_BASE}/history/{prompt_id}", timeout=10) as r:
        return json.loads(r.read())


async def wait_for_output(prompt_id: str, timeout: int = 300) -> list[Path]:
    """Poll until generation is done, return list of output image paths."""
    start = asyncio.get_event_loop().time()
    while True:
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError(f"ComfyUI generation timed out after {timeout}s")

        await asyncio.sleep(2)
        history = get_history(prompt_id)

        if prompt_id not in history:
            continue

        result = history[prompt_id]
        if result.get("status", {}).get("status_str") == "error":
            msgs = result.get("status", {}).get("messages", [])
            raise RuntimeError(f"ComfyUI generation error: {msgs}")

        outputs = result.get("outputs", {})
        images = []
        for node_output in outputs.values():
            for img in node_output.get("images", []):
                img_path = COMFYUI_OUTPUT_DIR / img["filename"]
                if img_path.exists():
                    images.append(img_path)

        if images:
            return images


def estimate_time(steps: int = 20, width: int = 1024, height: int = 1024) -> str:
    """Estimate generation time for Flux on Apple Silicon (M-series CPU+GPU)."""
    # Empirical: Flux Q4_K_S on M2/M3 ~2-4s per step at 1024x1024
    pixels = width * height
    base_seconds_per_step = 2.5  # M2 baseline
    if pixels > 1024 * 1024:
        base_seconds_per_step *= (pixels / (1024 * 1024)) ** 0.5
    total = steps * base_seconds_per_step
    if total < 60:
        return f"~{int(total)}s"
    return f"~{int(total/60)}min {int(total%60)}s"


async def generate(
    prompt: str,
    skill: str = "txt2img",
    preset: str | None = None,
    extra_params: dict | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """
    High-level: generate image from text prompt.

    Returns:
        {
            "prompt_id": str,
            "images": [Path, ...],
            "estimated_time": str,
            "skill": str,
            "preset": str | None,
            "prompt_used": str,
        }
    """
    if not is_running():
        raise RuntimeError("ComfyUI is not running on port 8188")

    workflow, schema = _load_skill(skill)
    params = _apply_preset(schema, preset, prompt)
    if extra_params:
        params.update(extra_params)

    workflow = _apply_params(workflow, schema, params)

    steps = params.get("steps", schema["parameters"].get("steps", {}).get("default", 20))
    width = params.get("width", schema["parameters"].get("width", {}).get("default", 1024))
    height = params.get("height", schema["parameters"].get("height", {}).get("default", 1024))
    est = estimate_time(steps, width, height)

    prompt_id = submit_workflow(workflow)
    images = await wait_for_output(prompt_id, timeout=timeout)

    return {
        "prompt_id": prompt_id,
        "images": images,
        "estimated_time": est,
        "skill": skill,
        "preset": preset,
        "prompt_used": params.get("prompt", prompt),
    }
