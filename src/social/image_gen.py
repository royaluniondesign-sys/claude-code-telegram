"""Brand-consistent Instagram post image generator.

Anthropic / RUD Agency brand identity:
  Background : #faf9f5  (cream)
  Dark       : #141413
  Orange     : #d97757  (accent — use sparingly)
  Mid-gray   : #b0aea5
  Light-gray : #e8e6dc
  Headline   : Space Grotesk Variable  (Styrene alternative)
  Body       : Lora Variable           (Tiempos alternative)

No emojis in graphics. Pure editorial typography.
"""
from __future__ import annotations

import io
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# ── Brand tokens ──────────────────────────────────────────────────────────────
CREAM      = (250, 249, 245)   # #faf9f5
DARK       = ( 20,  20,  19)   # #141413
ORANGE     = (217, 119,  87)   # #d97757
MID_GRAY   = (176, 174, 165)   # #b0aea5
LIGHT_GRAY = (232, 230, 220)   # #e8e6dc
DARK_GRAY  = ( 90,  88,  83)   # #5a5853  — body text

# ── Format registry ───────────────────────────────────────────────────────────
FORMATS: dict[str, Tuple[int, int]] = {
    "1:1":  (1080, 1080),   # Feed square
    "4:5":  (1080, 1350),   # Feed portrait
    "9:16": (1080, 1920),   # Stories / Reels cover
    "4:3":  (1080,  810),   # Landscape feed
    "16:9": (1920, 1080),   # YouTube / Wide
}

# ── Font paths (real Anthropic brand fonts) ───────────────────────────────────
_ASSETS = Path(__file__).parent.parent.parent / "assets" / "fonts"

# Anthropic brand fonts (provided by user)
_SERIF_DISPLAY  = _ASSETS / "AnthropicSerif-Display-Semibold-Static.otf"   # Big headlines
_SANS_TEXT      = _ASSETS / "AnthropicSans-Text-Regular-Static.otf"        # Body, labels, tags
_SERIF_WEB      = _ASSETS / "AnthropicSerif-WebText-Regular.ttf"           # Serif fallback (web)
_SANS_WEB       = _ASSETS / "AnthropicSans-WebText-Regular.ttf"            # Sans fallback (web)

# Last-resort fallbacks
_SYS_SANS  = ["/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"]
_SYS_SERIF = ["/Library/Fonts/Georgia.ttf"]


def _load(path: Path | str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except (OSError, IOError):
        return None  # type: ignore[return-value]


def _font_headline(size: int) -> ImageFont.FreeTypeFont:
    """Anthropic Serif Display Semibold for headlines."""
    for src in (_SERIF_DISPLAY, _SERIF_WEB):
        f = _load(src, size)
        if f:
            return f
    for p in _SYS_SERIF:
        f = _load(p, size)
        if f:
            return f
    return ImageFont.load_default(size=size)


def _font_label(size: int) -> ImageFont.FreeTypeFont:
    """Anthropic Sans Text Regular for tags, labels, brand mark."""
    for src in (_SANS_TEXT, _SANS_WEB):
        f = _load(src, size)
        if f:
            return f
    for p in _SYS_SANS:
        f = _load(p, size)
        if f:
            return f
    return ImageFont.load_default(size=size)


def _font_body(size: int) -> ImageFont.FreeTypeFont:
    """Anthropic Sans Text Regular for body / subheadline."""
    for src in (_SANS_TEXT, _SANS_WEB):
        f = _load(src, size)
        if f:
            return f
    for p in _SYS_SANS:
        f = _load(p, size)
        if f:
            return f
    return ImageFont.load_default(size=size)


# ── Text utilities ────────────────────────────────────────────────────────────
def _text_w(text: str, font: ImageFont.FreeTypeFont) -> int:
    """Pixel width of text."""
    dummy = Image.new("RGB", (1, 1))
    bb = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_px: int) -> list[str]:
    """Word-wrap text to fit max_px width."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        test = (cur + " " + word).strip()
        if _text_w(test, font) <= max_px:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _draw_text_block(
    draw: ImageDraw.Draw,
    lines: list[str],
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    color: tuple,
    line_height: float = 1.25,
) -> int:
    """Draw multi-line text, return y after last line."""
    size = font.size
    lh = int(size * line_height)
    for i, line in enumerate(lines):
        draw.text((x, y + i * lh), line, fill=color, font=font)
    return y + len(lines) * lh


# ── Post spec ─────────────────────────────────────────────────────────────────
@dataclass
class PostSpec:
    """Specification for one post image."""
    headline: str              # Bold statement, 1–2 lines
    subheadline: str = ""      # Supporting body, 2–3 lines
    caption: str = ""          # CTA / attribution near bottom
    tag: str = "AURA AI"       # Small category label top-left
    format: str = "1:1"        # Key from FORMATS dict
    slide_num: Optional[int] = None
    slide_total: Optional[int] = None


# ── Main generator ────────────────────────────────────────────────────────────
def generate_post_image(spec: PostSpec) -> bytes:
    """Generate a brand-consistent post image. Returns PNG bytes."""
    w, h = FORMATS.get(spec.format, (1080, 1080))
    img  = Image.new("RGB", (w, h), CREAM)
    draw = ImageDraw.Draw(img)

    pad      = max(64, int(w * 0.059))   # ~64px @ 1080
    max_text = w - pad * 2

    # ── Orange accent bar (top edge) ─────────────────────────────────────────
    bar_h = max(4, int(h * 0.004))
    draw.rectangle([0, 0, w, bar_h], fill=ORANGE)

    # ── Tag label ────────────────────────────────────────────────────────────
    tag_size = max(18, int(w * 0.020))
    tag_font = _font_label(tag_size)
    tag_text = spec.tag.upper()
    tag_y    = bar_h + pad // 2 + 4
    draw.text((pad, tag_y), tag_text, fill=ORANGE, font=tag_font)

    # ── Slide counter (top-right) ─────────────────────────────────────────────
    if spec.slide_num is not None and spec.slide_total:
        ctr_text = f"{spec.slide_num:02d} / {spec.slide_total:02d}"
        ctr_font = _font_label(tag_size)
        ctr_w    = _text_w(ctr_text, ctr_font)
        draw.text((w - pad - ctr_w, tag_y), ctr_text, fill=MID_GRAY, font=ctr_font)

    # ── Vertical layout: center the content block ──────────────────────────
    # Reserve top/bottom areas; place content block in the middle.
    reserved_top    = tag_y + tag_size + pad
    reserved_bottom = pad + int(h * 0.09)  # caption zone

    headline_size = max(60, int(w * 0.083))   # ~90px @ 1080
    sub_size      = max(32, int(w * 0.038))   # ~41px @ 1080
    cap_size      = max(22, int(w * 0.026))   # ~28px @ 1080

    h_font   = _font_headline(headline_size)
    sub_font = _font_body(sub_size)
    cap_font = _font_label(cap_size)

    h_lh  = int(headline_size * 1.15)
    sub_lh = int(sub_size * 1.45)
    div_h = max(3, int(h * 0.003))
    div_gap = int(h * 0.025)

    # Calculate content block height
    # Auto-shrink headline font to fit 3 lines max
    h_lines = _wrap(spec.headline, h_font, max_text)
    if len(h_lines) > 3:
        headline_size = max(48, int(headline_size * 0.82))
        h_font = _font_grotesk(headline_size)
        h_lh   = int(headline_size * 1.15)
        h_lines = _wrap(spec.headline, h_font, max_text)[:3]
    else:
        h_lines = h_lines[:3]
    sub_lines = _wrap(spec.subheadline, sub_font, max_text)[:2] if spec.subheadline else []

    content_h = (
        len(h_lines) * h_lh
        + div_gap + div_h + div_gap
        + len(sub_lines) * sub_lh
    )

    avail   = (h - reserved_top - reserved_bottom)
    start_y = reserved_top + max(0, (avail - content_h) // 2)

    # ── Headline ──────────────────────────────────────────────────────────────
    start_y = _draw_text_block(
        draw, h_lines, pad, start_y, h_font, DARK, line_height=h_lh / headline_size
    )

    # ── Orange divider line ───────────────────────────────────────────────────
    div_y     = start_y + div_gap
    div_width = int(w * 0.28)   # ~30% of width — short editorial rule
    draw.rectangle([pad, div_y, pad + div_width, div_y + div_h], fill=ORANGE)
    start_y = div_y + div_h + div_gap

    # ── Subheadline ───────────────────────────────────────────────────────────
    if sub_lines:
        _draw_text_block(
            draw, sub_lines, pad, start_y, sub_font, DARK_GRAY,
            line_height=sub_lh / sub_size,
        )

    # ── Bottom zone ───────────────────────────────────────────────────────────
    # Caption text (left-aligned, above brand mark)
    cap_lines = _wrap(spec.caption, cap_font, int(max_text * 0.75))[:2] if spec.caption else []
    brand_size  = max(18, int(w * 0.019))
    brand_font  = _font_label(brand_size)
    brand_text  = "RUD AGENCY"
    brand_w     = _text_w(brand_text, brand_font)

    # Thin separator above footer area
    footer_y = h - pad - brand_size - int(h * 0.01)
    sep_y    = footer_y - int(h * 0.025)
    draw.rectangle([pad, sep_y, w - pad, sep_y + 1], fill=LIGHT_GRAY)

    if cap_lines:
        cap_y = sep_y - div_gap - len(cap_lines) * int(cap_size * 1.4)
        _draw_text_block(
            draw, cap_lines, pad, cap_y, cap_font, MID_GRAY,
            line_height=1.4,
        )

    # Brand mark (bottom-right)
    draw.text((w - pad - brand_w, footer_y), brand_text, fill=MID_GRAY, font=brand_font)

    # Export PNG
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def generate_carousel(specs: list[PostSpec], fmt: str = "1:1") -> list[bytes]:
    """Generate images for a carousel, injecting slide numbers."""
    total  = len(specs)
    result = []
    for i, spec in enumerate(specs, 1):
        numbered = PostSpec(
            headline=spec.headline,
            subheadline=spec.subheadline,
            caption=spec.caption,
            tag=spec.tag,
            format=fmt,
            slide_num=i,
            slide_total=total,
        )
        result.append(generate_post_image(numbered))
    return result


def save_post_image(png_bytes: bytes, path: Path) -> Path:
    """Write PNG bytes to disk, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)
    return path
