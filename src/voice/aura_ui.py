"""AURA Voice UI — PyQt6 overlay interface.

Dark transparent HUD showing:
  - Live waveform / listening indicator
  - Transcript (user speech + AURA responses)
  - Active tool calls in real-time
  - System metrics (CPU, RAM, uptime)
  - Text input for typing commands
  - Drag-and-drop file analysis

Adapted from Mark XXXIX JarvisUI pattern for AURA.
"""
from __future__ import annotations

import asyncio
import math
import platform
import random
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import psutil
import requests
from PyQt6.QtCore import (
    QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal, QObject,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget,
)

# ── Palette ───────────────────────────────────────────────────────────────────
class C:
    BG       = "#00060a"
    PANEL    = "#010d14"
    PANEL2   = "#010f18"
    BORDER   = "#0d3347"
    BORDER_B = "#1a5c7a"
    PRI      = "#00d4ff"       # cyan
    PRI_DIM  = "#007a99"
    PRI_GHO  = "#001f2e"
    ACC      = "#c9a84c"       # gold (AURA brand)
    ACC2     = "#f5f0e8"       # cream
    GREEN    = "#00ff88"
    GREEN_D  = "#00aa55"
    RED      = "#ff3355"
    TEXT     = "#8ffcff"
    TEXT_DIM = "#3a8a9a"
    TEXT_MED = "#5ab8cc"
    WHITE    = "#d8f8ff"
    BAR_BG   = "#011520"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h)
    c.setAlpha(a)
    return c


_DAEMON_PORT = 8085
_POLL_MS     = 500      # status poll interval
_WAVE_STEPS  = 40       # waveform bars


# ── Waveform canvas ───────────────────────────────────────────────────────────
class WaveCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(60)
        self._bars = [0.0] * _WAVE_STEPS
        self._active = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(60)

    def set_active(self, active: bool):
        self._active = active

    def push_level(self, level: float):
        self._bars.append(min(level, 1.0))
        if len(self._bars) > _WAVE_STEPS:
            self._bars.pop(0)

    def _step(self):
        if self._active:
            # Animate with random noise when listening
            self._bars.append(random.uniform(0.1, 0.7))
            if len(self._bars) > _WAVE_STEPS:
                self._bars.pop(0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, qcol(C.BG))

        if not self._bars:
            return

        bar_w = W / _WAVE_STEPS
        mid = H // 2
        col_on  = qcol(C.PRI, 200)
        col_off = qcol(C.PRI_DIM, 80)

        for i, val in enumerate(self._bars):
            x = i * bar_w
            h = max(2, int(val * (H - 8)))
            col = col_on if self._active else col_off
            pen = QPen(col, max(1, bar_w * 0.6))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(
                QPointF(x + bar_w / 2, mid - h / 2),
                QPointF(x + bar_w / 2, mid + h / 2),
            )
        p.end()


# ── Metric bar ────────────────────────────────────────────────────────────────
class MetricBar(QWidget):
    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._pct   = 0.0
        self._text  = "0%"
        self.setFixedHeight(18)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._pct  = max(0.0, min(1.0, pct))
        self._text = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        r = 3

        # Background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol(C.BAR_BG))
        p.drawRoundedRect(0, 4, W, H - 8, r, r)

        # Fill
        fw = int(W * self._pct)
        if fw > 0:
            grad = QLinearGradient(0, 0, W, 0)
            grad.setColorAt(0, qcol(self._color, 180))
            grad.setColorAt(1, qcol(self._color, 100))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(0, 4, fw, H - 8, r, r)

        # Label
        p.setPen(qcol(C.WHITE))
        font = QFont("Menlo", 8)
        p.setFont(font)
        p.drawText(4, H - 6, f"{self._label}: {self._text}")
        p.end()


# ── Log widget ────────────────────────────────────────────────────────────────
class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 6px;
                font-family: Menlo, Monaco, monospace;
                font-size: 11px;
                padding: 6px;
            }}
        """)
        self._sig.connect(self._append)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _append(self, text: str):
        self.append(text)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


# ── Status dot ────────────────────────────────────────────────────────────────
class StatusDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self._on = False
        self._phase = 0.0
        t = QTimer(self)
        t.timeout.connect(self._pulse)
        t.start(50)

    def set_on(self, on: bool):
        self._on = on

    def _pulse(self):
        self._phase = (self._phase + 0.1) % (2 * math.pi)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._on:
            a = int(180 + 75 * math.sin(self._phase))
            col = qcol(C.GREEN, a)
        else:
            col = qcol(C.RED, 180)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(1, 1, 10, 10)
        p.end()


# ── Main window ───────────────────────────────────────────────────────────────
class AuraWindow(QMainWindow):
    _transcript_sig = pyqtSignal(str, str)    # speaker, text
    _tool_sig       = pyqtSignal(str, str)    # tool, result preview
    _status_sig     = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AURA — Voice Agent")
        self.setMinimumSize(860, 580)
        self.resize(980, 660)

        # Frameless + translucent
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._drag_pos = None
        self._setup_ui()
        self._connect_signals()

        # Poll daemon
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_daemon)
        self._poll_timer.start(_POLL_MS)

        # Keyboard shortcuts
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.close)
        QShortcut(QKeySequence("Ctrl+H"), self).activated.connect(self._toggle_minimize)

    def _setup_ui(self):
        root_w = QWidget()
        root_w.setObjectName("root")
        root_w.setStyleSheet(f"""
            #root {{
                background: rgba(0,6,10,230);
                border: 1px solid {C.BORDER_B};
                border-radius: 12px;
            }}
        """)
        self.setCentralWidget(root_w)

        root = QVBoxLayout(root_w)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)

        body.addWidget(self._build_left_panel(), stretch=0)
        body.addWidget(self._build_center_panel(), stretch=1)
        body.addWidget(self._build_right_panel(), stretch=0)

        root.addLayout(body)
        root.addWidget(self._build_footer())

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(44)
        w.setStyleSheet(f"background: {C.PANEL}; border-radius: 12px 12px 0 0; border-bottom: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 0, 14, 0)

        self._status_dot = StatusDot()
        lay.addWidget(self._status_dot)

        title = QLabel("✨  A U R A")
        title.setStyleSheet(f"color: {C.PRI}; font-family: Menlo; font-size: 14px; font-weight: bold; letter-spacing: 4px;")
        lay.addWidget(title)

        self._status_label = QLabel("CONNECTING...")
        self._status_label.setStyleSheet(f"color: {C.TEXT_DIM}; font-family: Menlo; font-size: 10px;")
        lay.addWidget(self._status_label)

        lay.addStretch()

        self._model_label = QLabel("gemini-2.5-flash-native-audio")
        self._model_label.setStyleSheet(f"color: {C.ACC}; font-family: Menlo; font-size: 9px;")
        lay.addWidget(self._model_label)

        lay.addSpacing(12)

        btn_min = QPushButton("─")
        btn_close = QPushButton("✕")
        for btn in (btn_min, btn_close):
            btn.setFixedSize(24, 24)
            btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none; font-size: 12px;")
        btn_min.clicked.connect(self.showMinimized)
        btn_close.clicked.connect(self.close)
        lay.addWidget(btn_min)
        lay.addWidget(btn_close)
        return w

    # ── Left panel: metrics ───────────────────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        w = QFrame()
        w.setFixedWidth(150)
        w.setStyleSheet(f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 8px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(8)

        lbl = QLabel("SYSTEM")
        lbl.setStyleSheet(f"color: {C.PRI_DIM}; font-family: Menlo; font-size: 9px; letter-spacing: 2px;")
        lay.addWidget(lbl)

        self._cpu_bar  = MetricBar("CPU",  C.PRI)
        self._ram_bar  = MetricBar("RAM",  C.ACC)
        self._uptime_bar = MetricBar("UP", C.GREEN)
        lay.addWidget(self._cpu_bar)
        lay.addWidget(self._ram_bar)
        lay.addWidget(self._uptime_bar)

        lay.addSpacing(8)
        lbl2 = QLabel("TOOLS FIRED")
        lbl2.setStyleSheet(f"color: {C.PRI_DIM}; font-family: Menlo; font-size: 9px; letter-spacing: 2px;")
        lay.addWidget(lbl2)

        self._tool_count = QLabel("0")
        self._tool_count.setStyleSheet(f"color: {C.ACC}; font-family: Menlo; font-size: 28px; font-weight: bold;")
        self._tool_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._tool_count)

        lay.addSpacing(8)
        lbl3 = QLabel("TURNS")
        lbl3.setStyleSheet(f"color: {C.PRI_DIM}; font-family: Menlo; font-size: 9px; letter-spacing: 2px;")
        lay.addWidget(lbl3)

        self._turn_count = QLabel("0")
        self._turn_count.setStyleSheet(f"color: {C.PRI}; font-family: Menlo; font-size: 28px; font-weight: bold;")
        self._turn_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._turn_count)

        lay.addStretch()

        # Start/Stop button
        self._toggle_btn = QPushButton("⏹ STOP")
        self._toggle_btn.setFixedHeight(30)
        self._toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO};
                color: {C.RED};
                border: 1px solid {C.RED};
                border-radius: 4px;
                font-family: Menlo;
                font-size: 10px;
            }}
            QPushButton:hover {{ background: {C.RED}; color: {C.BG}; }}
        """)
        self._toggle_btn.clicked.connect(self._toggle_agent)
        lay.addWidget(self._toggle_btn)
        return w

    # ── Center panel: waveform + transcript ───────────────────────────────────

    def _build_center_panel(self) -> QWidget:
        w = QFrame()
        w.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 8px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Waveform
        self._wave = WaveCanvas()
        lay.addWidget(self._wave)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};")
        lay.addWidget(sep)

        # Transcript
        trans_lbl = QLabel("TRANSCRIPT")
        trans_lbl.setStyleSheet(f"color: {C.PRI_DIM}; font-family: Menlo; font-size: 9px; letter-spacing: 2px;")
        lay.addWidget(trans_lbl)

        self._transcript = LogWidget()
        self._transcript.setMinimumHeight(300)
        lay.addWidget(self._transcript, stretch=1)

        # Input
        lay.addWidget(self._build_input_row())
        return w

    def _build_input_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Escribe un comando o habla al micrófono...")
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL2};
                color: {C.WHITE};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
                padding: 6px 10px;
                font-family: Menlo;
                font-size: 12px;
            }}
            QLineEdit:focus {{ border-color: {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send_text)
        lay.addWidget(self._input)

        send_btn = QPushButton("▶")
        send_btn.setFixedSize(34, 34)
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO};
                color: {C.PRI};
                border: 1px solid {C.PRI};
                border-radius: 6px;
                font-size: 14px;
            }}
            QPushButton:hover {{ background: {C.PRI}; color: {C.BG}; }}
        """)
        send_btn.clicked.connect(self._send_text)
        lay.addWidget(send_btn)
        return w

    # ── Right panel: tool log ─────────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        w = QFrame()
        w.setFixedWidth(220)
        w.setStyleSheet(f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 8px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        lbl = QLabel("TOOL CALLS")
        lbl.setStyleSheet(f"color: {C.PRI_DIM}; font-family: Menlo; font-size: 9px; letter-spacing: 2px;")
        lay.addWidget(lbl)

        self._tool_log = LogWidget()
        lay.addWidget(self._tool_log, stretch=1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};")
        lay.addWidget(sep)

        lbl2 = QLabel("LAST ACTION")
        lbl2.setStyleSheet(f"color: {C.PRI_DIM}; font-family: Menlo; font-size: 9px; letter-spacing: 2px;")
        lay.addWidget(lbl2)

        self._last_action = QLabel("—")
        self._last_action.setWordWrap(True)
        self._last_action.setStyleSheet(f"color: {C.ACC}; font-family: Menlo; font-size: 10px;")
        lay.addWidget(self._last_action)

        return w

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(28)
        w.setStyleSheet(f"background: {C.PANEL}; border-radius: 0 0 12px 12px; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 0, 14, 0)

        self._footer_label = QLabel("AURA v2 · Gemini 2.5 Flash Native Audio · 52 tools · AURA + Hermes + Mac")
        self._footer_label.setStyleSheet(f"color: {C.TEXT_DIM}; font-family: Menlo; font-size: 9px;")
        lay.addWidget(self._footer_label)
        lay.addStretch()

        shortcut = QLabel("Ctrl+Q: cerrar  |  Ctrl+H: minimizar")
        shortcut.setStyleSheet(f"color: {C.BORDER_B}; font-family: Menlo; font-size: 9px;")
        lay.addWidget(shortcut)
        return w

    # ── Signal connections ────────────────────────────────────────────────────

    def _connect_signals(self):
        self._transcript_sig.connect(self._on_transcript)
        self._tool_sig.connect(self._on_tool)
        self._status_sig.connect(self._on_status)

    # ── Slots (main thread) ───────────────────────────────────────────────────

    def _on_transcript(self, speaker: str, text: str):
        if speaker == "aura":
            self._transcript.append_log(f'<span style="color:{C.PRI}">✨ AURA:</span> <span style="color:{C.WHITE}">{text}</span>')
            self._wave.set_active(False)
        else:
            self._transcript.append_log(f'<span style="color:{C.ACC}">🎤 TÚ:</span> <span style="color:{C.TEXT_MED}">{text}</span>')
            self._wave.set_active(True)
        turns = int(self._turn_count.text()) + 1
        self._turn_count.setText(str(turns))

    def _on_tool(self, tool: str, preview: str):
        color = C.GREEN if "error" not in preview.lower() else C.RED
        self._tool_log.append_log(f'<span style="color:{color}">🔧 {tool}</span>')
        self._last_action.setText(f"🔧 {tool}")
        count = int(self._tool_count.text()) + 1
        self._tool_count.setText(str(count))

    def _on_status(self, status: dict):
        running = status.get("status") == "running"
        self._status_dot.set_on(running)
        self._status_label.setText(status.get("status", "—").upper())
        self._wave.set_active(running)
        self._turn_count.setText(str(status.get("transcript_count", 0)))

        uptime = status.get("uptime_s", 0)
        h, m = divmod(uptime // 60, 60)
        self._uptime_bar.set_value(
            min(uptime / 3600, 1.0),
            f"{h}h{m:02d}m" if h else f"{uptime // 60}m{uptime % 60:02d}s"
        )

        if running:
            self._toggle_btn.setText("⏹ STOP")
            self._toggle_btn.setStyleSheet(self._toggle_btn.styleSheet().replace(C.GREEN, C.RED))
        else:
            self._toggle_btn.setText("▶ START")

    # ── Daemon polling ────────────────────────────────────────────────────────

    def _poll_daemon(self):
        # Update system metrics
        self._cpu_bar.set_value(psutil.cpu_percent() / 100, f"{psutil.cpu_percent():.0f}%")
        mem = psutil.virtual_memory()
        self._ram_bar.set_value(mem.percent / 100, f"{mem.percent:.0f}%")

        # Poll daemon status
        def _fetch():
            try:
                r = requests.get(f"http://127.0.0.1:{_DAEMON_PORT}/status", timeout=2)
                if r.status_code == 200:
                    self._status_sig.emit(r.json())
            except Exception:
                self._status_sig.emit({"status": "offline"})

        threading.Thread(target=_fetch, daemon=True).start()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _send_text(self):
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self._transcript.append_log(f'<span style="color:{C.ACC}">💬 TEXTO:</span> <span style="color:{C.TEXT_MED}">{text}</span>')

        def _post():
            try:
                requests.post(
                    f"http://127.0.0.1:{_DAEMON_PORT}/send",
                    json={"text": text},
                    timeout=5,
                )
            except Exception as e:
                self._transcript_sig.emit("system", f"Error: {e}")

        threading.Thread(target=_post, daemon=True).start()

    def _toggle_agent(self):
        def _call(endpoint):
            try:
                requests.post(f"http://127.0.0.1:{_DAEMON_PORT}/{endpoint}", json={}, timeout=35)
            except Exception:
                pass

        status_text = self._status_label.text().lower()
        if status_text == "running":
            threading.Thread(target=_call, args=("stop",), daemon=True).start()
            self._toggle_btn.setText("▶ START")
        else:
            threading.Thread(target=_call, args=("start",), daemon=True).start()
            self._toggle_btn.setText("⟳ STARTING...")

    def _toggle_minimize(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.showMinimized()

    # ── Drag to move (frameless) ──────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    # ── Public API for voice daemon to push events ────────────────────────────

    def push_transcript(self, speaker: str, text: str):
        self._transcript_sig.emit(speaker, text)

    def push_tool(self, tool: str, preview: str = ""):
        self._tool_sig.emit(tool, preview)


# ── Entry point ───────────────────────────────────────────────────────────────

def run_ui():
    """Launch the AURA UI. Connects to running voice daemon via HTTP."""
    app = QApplication(sys.argv)
    app.setApplicationName("AURA Voice")

    # High-DPI
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    win = AuraWindow()
    # Center window on primary screen
    screen = app.primaryScreen().geometry()
    win.move(screen.center().x() - 500, screen.center().y() - 300)
    win.show()
    win.raise_()
    win.activateWindow()
    app.setActiveWindow(win)

    # macOS: set regular app policy so window appears and app is in Dock
    try:
        import AppKit  # type: ignore[import]
        AppKit.NSApp.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular
    except Exception:
        pass

    # macOS: use osascript to bring process to front (most reliable method)
    import os as _os, subprocess as _sub, threading as _thr
    def _activate():
        import time as _time
        _time.sleep(0.5)  # let window render first
        _sub.Popen(
            ["osascript", "-e",
             f'tell application "System Events" to set frontmost of '
             f'(first process whose unix id is {_os.getpid()}) to true'],
            stdout=_sub.DEVNULL, stderr=_sub.DEVNULL,
        )
    _thr.Thread(target=_activate, daemon=True).start()

    # Connect transcript polling (separate from status poll)
    def _poll_transcript():
        try:
            r = requests.get(
                f"http://127.0.0.1:{_DAEMON_PORT}/transcript?limit=50",
                timeout=2
            )
            if r.status_code == 200:
                entries = r.json().get("transcript", [])
                # Only new entries (tracked by count)
                if not hasattr(_poll_transcript, "_seen"):
                    _poll_transcript._seen = 0
                new = entries[_poll_transcript._seen:]
                for e in new:
                    win.push_transcript(e["speaker"], e["text"])
                _poll_transcript._seen = len(entries)
        except Exception:
            pass

    trans_timer = QTimer()
    trans_timer.timeout.connect(_poll_transcript)
    trans_timer.start(800)

    sys.exit(app.exec())


if __name__ == "__main__":
    run_ui()
