"""Computer control via PyAutoGUI + AI screen finder — adapted from Mark XXXIX.

Full keyboard/mouse automation + Gemini-powered element detection.
Mac-first (uses osascript for window focus, AVFoundation for camera).
"""
from __future__ import annotations

import random
import re
import string
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

_PYAUTOGUI_OK = False
_PYPERCLIP_OK = False

try:
    import pyautogui
    pyautogui.FAILSAFE = True   # move mouse to corner to abort
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI_OK = True
except ImportError:
    pass

try:
    import pyperclip
    _PYPERCLIP_OK = True
except ImportError:
    pass


def _require_pyautogui() -> None:
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")


# ── Primitive actions ─────────────────────────────────────────────────────────

def type_text(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.2)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:80]}{'…' if len(text) > 80 else ''}"


def smart_type(text: str, clear_first: bool = True) -> str:
    """Type using clipboard for long strings (faster, more reliable)."""
    _require_pyautogui()
    if clear_first:
        clear_field()
        time.sleep(0.1)
    if len(text) > 20 and _PYPERCLIP_OK:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("command", "v")  # Mac uses Cmd+V
        return f"Smart-typed (clipboard): {text[:80]}{'…' if len(text) > 80 else ''}"
    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:80]}{'…' if len(text) > 80 else ''}"


def click(x: Optional[int] = None, y: Optional[int] = None,
          button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x},{y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked current pos [{button}]"


def right_click(x: Optional[int] = None, y: Optional[int] = None) -> str:
    return click(x, y, button="right")


def double_click(x: Optional[int] = None, y: Optional[int] = None) -> str:
    return click(x, y, clicks=2)


def move_mouse(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x},{y})"


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def hotkey(*keys: str) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def press_key(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    clicks = amount if direction in ("up", "right") else -amount
    if direction in ("up", "down"):
        pyautogui.scroll(clicks)
    else:
        pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def clear_field() -> str:
    _require_pyautogui()
    pyautogui.hotkey("command", "a")  # Mac: Cmd+A
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"


def get_clipboard() -> str:
    if _PYPERCLIP_OK:
        return pyperclip.paste()
    hotkey("command", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def set_clipboard(text: str) -> str:
    if _PYPERCLIP_OK:
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        pyautogui.hotkey("command", "v")
        return f"Pasted: {text[:80]}{'…' if len(text) > 80 else ''}"
    return "pyperclip not available"


def take_screenshot(save_path: Optional[str] = None) -> str:
    _require_pyautogui()
    path = Path(save_path) if save_path else Path.home() / "Desktop" / "aura_screenshot.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def focus_window(title: str) -> str:
    """Bring window with matching title to foreground (macOS via osascript)."""
    script = (
        f'tell application "System Events" to '
        f'set frontmost of (first process whose name contains "{title}") to true'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)
        return f"Focused: {title}"
    except Exception as e:
        return f"focus_window failed: {e}"


def open_application(app_name: str) -> str:
    """Open an app by name on macOS."""
    try:
        subprocess.run(["open", "-a", app_name], timeout=5)
        time.sleep(1)
        return f"Opened: {app_name}"
    except Exception as e:
        return f"open_application failed: {e}"


# ── AI-powered screen finder ──────────────────────────────────────────────────

def screen_find(description: str, api_key: str) -> Optional[Tuple[int, int]]:
    """Use Gemini Flash Lite to locate a UI element by description.

    Takes a screenshot, asks Gemini where the element is, returns (x, y).
    Free model (gemini-2.5-flash-lite) — minimal cost.
    """
    try:
        from google import genai  # type: ignore[import]
        from google.genai import types as gtypes  # type: ignore[import]
        import io as _io
    except ImportError:
        return None

    _require_pyautogui()
    w, h = pyautogui.size()
    img = pyautogui.screenshot()
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    client = genai.Client(api_key=api_key)
    prompt = (
        f"This is a {w}×{h}px screenshot. Find: '{description}'. "
        f"Reply ONLY with center coords as: x,y  "
        f"If not visible: NOT_FOUND"
    )
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite-preview-06-17",
            contents=[
                gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
        )
        text = (resp.text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None
        m = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def screen_click(description: str, api_key: str) -> str:
    """Find element by description and click it."""
    coords = screen_find(description, api_key)
    if coords:
        time.sleep(0.2)
        click(x=coords[0], y=coords[1])
        return f"Clicked '{description}' at {coords}"
    return f"Element not found: '{description}'"


# ── Random data generator (for form filling) ──────────────────────────────────

_FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Blake", "Quinn"]
_LAST  = ["Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis"]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me"]


def random_data(data_type: str) -> str:
    dt = data_type.lower().strip()
    if dt == "first_name":  return random.choice(_FIRST)
    if dt == "last_name":   return random.choice(_LAST)
    if dt == "name":        return f"{random.choice(_FIRST)} {random.choice(_LAST)}"
    if dt == "email":
        return f"{random.choice(_FIRST).lower()}.{random.choice(_LAST).lower()}{random.randint(10,999)}@{random.choice(_DOMAINS)}"
    if dt == "username":    return f"{random.choice(_FIRST).lower()}{random.randint(100,9999)}"
    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw = random.choice(string.ascii_uppercase) + random.choice(string.digits) + "".join(random.choices(chars, k=10))
        return "".join(random.sample(raw, len(raw)))
    if dt == "phone":       return f"+1{random.randint(200,999)}{random.randint(1_000_000,9_999_999)}"
    if dt == "birthday":
        return f"{random.randint(1,12):02d}/{random.randint(1,28):02d}/{random.randint(1980,2000)}"
    return f"random_{data_type}_{random.randint(1000,9999)}"


# ── Main dispatch ─────────────────────────────────────────────────────────────

def computer_control(action: str, params: dict, gemini_api_key: str = "") -> str:
    """Dispatch computer control action. All Mark XXXIX actions supported.

    Actions: type, smart_type, click, double_click, right_click, move,
             drag, hotkey, press, scroll, copy, paste, screenshot, wait,
             clear_field, focus_window, open_app, screen_find, screen_click,
             random_data
    """
    try:
        a = action.lower().strip()

        if a == "type":
            return type_text(params.get("text", ""))
        if a == "smart_type":
            return smart_type(params.get("text", ""), params.get("clear_first", True))
        if a in ("click", "left_click"):
            return click(params.get("x"), params.get("y"), "left", 1)
        if a == "double_click":
            return double_click(params.get("x"), params.get("y"))
        if a == "right_click":
            return right_click(params.get("x"), params.get("y"))
        if a == "move":
            return move_mouse(int(params["x"]), int(params["y"]))
        if a == "drag":
            return drag(int(params["x1"]), int(params["y1"]),
                        int(params["x2"]), int(params["y2"]))
        if a == "hotkey":
            raw = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return hotkey(*keys)
        if a == "press":
            return press_key(params.get("key", "enter"))
        if a == "scroll":
            return scroll(params.get("direction", "down"), int(params.get("amount", 3)))
        if a == "copy":
            return get_clipboard()
        if a == "paste":
            return set_clipboard(params.get("text", ""))
        if a == "screenshot":
            return take_screenshot(params.get("path"))
        if a == "wait":
            secs = min(float(params.get("seconds", 1)), 30)
            time.sleep(secs)
            return f"Waited {secs}s"
        if a == "clear_field":
            return clear_field()
        if a == "focus_window":
            return focus_window(params.get("title", ""))
        if a == "open_app":
            return open_application(params.get("app", params.get("name", "")))
        if a == "screen_find":
            coords = screen_find(params.get("description", ""), gemini_api_key)
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"
        if a == "screen_click":
            return screen_click(params.get("description", ""), gemini_api_key)
        if a == "random_data":
            return random_data(params.get("type", "name"))

        return f"Unknown action: '{action}'"

    except Exception as e:
        return f"computer_control '{action}' error: {e}"
