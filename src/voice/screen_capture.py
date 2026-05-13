"""Screen & camera capture — adapted from Mark XXXIX for AURA.

Captures screen or webcam, compresses to JPEG for Gemini vision.
No Gemini dependency here — pure capture/compress utilities.
"""
from __future__ import annotations

import io
from typing import Optional, Tuple

_MSS_OK = False
_PIL_OK = False
_CV2_OK = False

try:
    import mss
    import mss.tools
    _MSS_OK = True
except ImportError:
    pass

try:
    import PIL.Image
    _PIL_OK = True
except ImportError:
    pass

try:
    import cv2  # type: ignore[import]
    import numpy as np  # type: ignore[import]
    _CV2_OK = True
except ImportError:
    pass

# Compression settings — keeps Gemini token cost low
_MAX_W = 1280
_MAX_H = 720
_JPEG_Q = 70


def _compress(img_bytes: bytes, src_fmt: str = "PNG") -> Tuple[bytes, str]:
    """Resize + compress to JPEG. Falls back to raw if Pillow unavailable."""
    if not _PIL_OK:
        return img_bytes, f"image/{src_fmt.lower()}"
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_MAX_W, _MAX_H), PIL.Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return img_bytes, f"image/{src_fmt.lower()}"


def capture_screen(monitor: int = 1) -> Tuple[bytes, str]:
    """Capture primary screen. Returns (jpeg_bytes, mime_type).

    Args:
        monitor: 1 = primary monitor, 0 = all monitors combined
    """
    if not _MSS_OK:
        raise RuntimeError("mss not installed. Run: pip install mss")
    with mss.MSS() as sct:
        monitors = sct.monitors
        target = monitors[monitor] if len(monitors) > monitor else monitors[0]
        shot = sct.grab(target)
        png = mss.tools.to_png(shot.rgb, shot.size)
    return _compress(png, "PNG")


def capture_region(x: int, y: int, w: int, h: int) -> Tuple[bytes, str]:
    """Capture a specific screen region."""
    if not _MSS_OK:
        raise RuntimeError("mss not installed")
    with mss.MSS() as sct:
        region = {"left": x, "top": y, "width": w, "height": h}
        shot = sct.grab(region)
        png = mss.tools.to_png(shot.rgb, shot.size)
    return _compress(png, "PNG")


def capture_camera(index: int = 0) -> Tuple[bytes, str]:
    """Capture a frame from webcam. Returns (jpeg_bytes, mime_type)."""
    if not _CV2_OK:
        raise RuntimeError("opencv-python not installed. Run: pip install opencv-python")

    # AVFoundation backend on macOS for best compatibility
    try:
        import platform
        backend = cv2.CAP_AVFOUNDATION if platform.system() == "Darwin" else cv2.CAP_ANY
    except Exception:
        backend = 0

    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera index {index}")

    # Warmup frames
    for _ in range(5):
        cap.read()
    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame")

    if _PIL_OK:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_MAX_W, _MAX_H), PIL.Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf_arr = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf_arr.tobytes(), "image/jpeg"


def screenshot_to_base64(monitor: int = 1) -> str:
    """Capture screen and return as base64 string (for Telegram/API use)."""
    import base64
    img_bytes, mime = capture_screen(monitor)
    b64 = base64.b64encode(img_bytes).decode()
    return f"data:{mime};base64,{b64}"
