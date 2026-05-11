"""Gemini Live Voice Agent — real-time bidirectional audio with full AURA tool access.

Uses Gemini 2.5 Flash Native Audio Preview (FREE tier, 1500 req/day).
Audio processing is native — no separate STT/TTS pipeline.

Architecture:
  Microphone → Gemini Live API → tool calls → AURA/Hermes/Computer tools
                               ↓
                         Mac speaker (audio response)
                         + optional Telegram notification

Based on Mark XXXIX pattern, adapted for AURA infrastructure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog

logger = structlog.get_logger()

# Model — Gemini 2.5 Flash Native Audio Preview (confirmed working)
_LIVE_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

# Audio config
_CHANNELS = 1
_SEND_SAMPLE_RATE = 16000    # mic input
_RECEIVE_SAMPLE_RATE = 24000  # speaker output
_CHUNK_SIZE = 1024

# System prompt for voice agent
def _build_system_prompt() -> str:
    """Build system prompt with live context from memory files."""
    import subprocess
    memory = ""
    obsidian = ""
    try:
        memory = subprocess.check_output(
            ["cat", "/Users/oxyzen/.aura/memory/MEMORY.md"],
            timeout=3, text=True, stderr=subprocess.DEVNULL
        )[:2000]
    except Exception:
        pass
    try:
        obsidian = subprocess.check_output(
            ["cat", "/Users/oxyzen/Obsidian/AURA_Dashboard.md"],
            timeout=3, text=True, stderr=subprocess.DEVNULL
        )[:2000]
    except Exception:
        pass

    return f"""Eres AURA — la IA personal de Ricardo Pinto, corriendo en su Mac 24/7.

MODO ACTUAL: VOZ EN TIEMPO REAL
- Estás hablando por voz, no por chat. Responde oralmente, de forma natural y fluida.
- Respuestas cortas por defecto (1-3 frases). Si la respuesta es larga, divídela en partes y pregunta si quiere más.
- NO digas "aquí va la lista" sin decirla. NO prometas enviar algo — DILO en voz.
- Pronuncia números, siglas y código de forma natural para el oído.

PERSONALIDAD:
- Directa, inteligente, con humor seco. Sin entusiasmo forzado ni "¡Por supuesto!".
- Habla en el idioma que te hablen (español por defecto con Ricardo).

IDENTIDAD DEL SISTEMA:
- Motor de voz: Gemini 2.5 Flash Native Audio
- Agente hermano: Hermes (@rudserverbot) — úsalo con hermes_ask para delegarle tareas
- Memoria: memory_search / memory_store
- Vault Obsidian: ~/Obsidian/ (compartido con Hermes)
- Control del Mac: computer_control, screen_capture

MEMORIA ACTUAL:
{memory if memory else "(no disponible)"}

ESTADO DEL SISTEMA:
{obsidian if obsidian else "(no disponible)"}

REGLAS DE EJECUCIÓN:
1. Ejecuta sin pedir confirmación (excepto: borrar datos, force push, gastar dinero)
2. Verifica con herramientas antes de responder sobre estado del sistema
3. Si la tarea es larga, avisa en voz y usa bash_run / hermes_task
4. Para ver pantalla → screen_capture. Para controlar Mac → computer_control

Sé concisa. Lo que dices se escucha en voz alta."""


_SYSTEM_PROMPT = _build_system_prompt()


class GeminiLiveAgent:
    """Manages the Gemini Live bidirectional audio session with full tool access."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        on_transcript: Optional[Callable[[str, str], None]] = None,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
        bot_token: str = "",
        owner_chat_id: str = "",
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._on_transcript = on_transcript   # cb(speaker, text)
        self._on_tool_call = on_tool_call     # cb(tool_name, args)

        # Thread + event loop for the session
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._out_queue: Optional[asyncio.Queue] = None
        self._audio_in_queue: Optional[asyncio.Queue] = None
        self._ready_evt = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        # Echo cancellation — exact pattern from Mark-XXXIX
        self._speaking_lock = threading.Lock()
        self._is_speaking = False
        # Text injection flag
        self._text_pending = False
        # Turn-done coordination (Mark-XXXIX pattern) — prevents audio cutoff
        self._turn_done_event: Optional[asyncio.Event] = None
        # Sleep/wake mode — mic muted while sleeping
        self._sleeping = False
        self._sleep_lock = threading.Lock()
        self._last_user_activity = time.monotonic()
        self._auto_sleep_secs = 120.0  # 2 min idle → auto-sleep

        # Tool executor
        from src.voice.tool_bridge import ToolExecutor
        self._executor = ToolExecutor(
            gemini_api_key=self._api_key,
            telegram_bot_token=bot_token,
            telegram_chat_id=owner_chat_id,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, timeout: float = 30.0) -> None:
        """Start the voice agent in a background thread."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._running = True
            self._ready_evt.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="GeminiLiveAgent",
            )
            self._thread.start()

        if not self._ready_evt.wait(timeout=timeout):
            raise RuntimeError(f"Gemini Live session did not connect within {timeout}s")
        logger.info("gemini_live_ready")

    def stop(self) -> None:
        """Stop the voice agent gracefully."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("gemini_live_stopped")

    def is_ready(self) -> bool:
        return self._session is not None and self._ready_evt.is_set()

    def send_text(self, text: str) -> None:
        """Send a text message to the voice session (for Telegram → voice bridge)."""
        if not self._loop or not self._out_queue:
            return
        asyncio.run_coroutine_threadsafe(
            self._out_queue.put(("text", text)),
            self._loop,
        )

    def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Feed a raw PCM chunk to the session (16kHz, mono, int16)."""
        if not self._loop or not self._out_queue:
            return
        asyncio.run_coroutine_threadsafe(
            self._out_queue.put(("audio", pcm_bytes)),
            self._loop,
        )

    def send_image(self, image_bytes: bytes, mime_type: str, text: str = "") -> None:
        """Send an image (screen/camera) + optional question to the session."""
        if not self._loop or not self._out_queue:
            return
        asyncio.run_coroutine_threadsafe(
            self._out_queue.put(("image", (image_bytes, mime_type, text))),
            self._loop,
        )

    def sleep(self) -> None:
        """Mute microphone — AURA goes quiet (e.g. user watching a video)."""
        with self._sleep_lock:
            self._sleeping = True
        logger.info("aura_sleeping")

    def wake(self) -> None:
        """Unmute microphone — AURA wakes up and listens."""
        with self._sleep_lock:
            self._sleeping = False
            self._last_user_activity = time.monotonic()
        # Notify Gemini that AURA has been woken up
        self.send_text("Ricardo te ha activado. Saluda brevemente.")
        logger.info("aura_waking")

    def is_sleeping(self) -> bool:
        with self._sleep_lock:
            return self._sleeping

    # ── Thread entry ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session_loop())
        except Exception as e:
            logger.error("gemini_live_loop_error", error=str(e))

    # ── Core session loop ─────────────────────────────────────────────────────

    async def _session_loop(self) -> None:
        try:
            from google import genai  # type: ignore[import]
            from google.genai import types as gtypes  # type: ignore[import]
        except ImportError:
            logger.error("google-genai not installed. Run: pip install google-genai")
            return

        from src.voice.tool_bridge import build_gemini_tools

        # Disable WebSocket keepalive pings.
        # The websockets library sends PING frames every 20s; if Gemini's server
        # doesn't respond within ping_timeout the library raises
        # 'keepalive ping timeout; no close frame received' and kills the session.
        # Fix: pass ping_interval=None via async_client_args — the SDK filters
        # these kwargs through _ensure_websocket_ssl_ctx before passing to
        # websockets.asyncio.client.connect (confirmed valid in websockets 16.0).
        # v1beta — exact as Mark-XXXIX
        client = genai.Client(
            api_key=self._api_key,
            http_options={
                "api_version": "v1beta",
                "async_client_args": {
                    "ping_interval": None,
                    "ping_timeout": None,
                },
            },
        )

        tools = build_gemini_tools()

        # Config — exact as Mark-XXXIX _build_config()
        config = gtypes.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=_SYSTEM_PROMPT,
            tools=tools,
            session_resumption=gtypes.SessionResumptionConfig(),
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name="Aoede"
                    )
                )
            ),
        )

        backoff = 5.0
        while self._running:
            try:
                logger.info("gemini_live_connecting")
                async with client.aio.live.connect(model=_LIVE_MODEL, config=config) as session:
                    self._session = session
                    self._out_queue = asyncio.Queue(maxsize=10)
                    self._audio_in_queue = asyncio.Queue()
                    self._turn_done_event = asyncio.Event()
                    self._ready_evt.set()
                    backoff = 2.0
                    logger.info("gemini_live_connected")

                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_loop(session))
                        tg.create_task(self._recv_loop(session))
                        tg.create_task(self._play_loop())
                        tg.create_task(self._mic_loop(session))
                        tg.create_task(self._auto_sleep_loop())

            except* Exception as eg:
                for exc in eg.exceptions:
                    logger.warning("gemini_live_session_error", error=str(exc))
            finally:
                self._session = None
                self._ready_evt.clear()

            if not self._running:
                break
            logger.info("gemini_live_reconnecting", backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)

    # ── Send loop ─────────────────────────────────────────────────────────────

    async def _send_loop(self, session: Any) -> None:
        """Forward queued messages to Gemini — exact pattern from Mark-XXXIX.

        Queue contains two types:
        - dict {"data": bytes, "mime_type": "audio/pcm"} — from mic callback
        - tuple ("text"|"image", ...) — from Telegram/HTTP injection
        """
        try:
            from google.genai import types as gtypes  # type: ignore[import]
        except ImportError:
            return

        while self._running:
            try:
                item = await asyncio.wait_for(self._out_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                # Mic audio — dict from callback, send as media (Mark-XXXIX pattern)
                if isinstance(item, dict):
                    await session.send_realtime_input(media=item)

                # Text injection from Telegram
                elif isinstance(item, tuple) and item[0] == "text":
                    self._text_pending = True
                    logger.info("sending_text_to_gemini", chars=len(item[1]))
                    await session.send_client_content(
                        turns=gtypes.Content(
                            role="user",
                            parts=[gtypes.Part(text=item[1])],
                        ),
                        turn_complete=True,
                    )
                    self._text_pending = False

                # Image injection
                elif isinstance(item, tuple) and item[0] == "image":
                    img_bytes, mime, text = item[1]
                    import base64
                    b64 = base64.b64encode(img_bytes).decode()
                    parts: list = [{"inline_data": {"mime_type": mime, "data": b64}}]
                    if text:
                        parts.append({"text": text})
                    await session.send_client_content(
                        turns={"parts": parts},
                        turn_complete=True,
                    )
            except Exception as e:
                logger.warning("gemini_live_send_error", error=str(e))

    # ── Receive loop ──────────────────────────────────────────────────────────

    async def _recv_loop(self, session: Any) -> None:
        """Receive responses: audio chunks, transcripts, tool calls.
        while True wraps session.receive() — exact pattern from Mark-XXXIX.
        """
        transcript: list[str] = []
        audio_chunks_received = 0

        while True:
            async for response in session.receive():
                # Audio data → playback queue
                if response.data:
                    audio_chunks_received += 1
                    if audio_chunks_received == 1:
                        logger.info("audio_data_first_chunk", bytes=len(response.data))
                    if self._audio_in_queue:
                        # Clear turn_done if new audio arrives mid-turn
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        await self._audio_in_queue.put(response.data)

                sc = response.server_content
                if sc:
                    # Transcript chunks
                    if sc.output_transcription and sc.output_transcription.text:
                        chunk = sc.output_transcription.text.strip()
                        if chunk:
                            transcript.append(chunk)

                    # User speech transcript
                    if sc.input_transcription and sc.input_transcription.text:
                        user_text = sc.input_transcription.text.strip()
                        if user_text:
                            self._last_user_activity = time.monotonic()
                            if self._on_transcript:
                                self._on_transcript("user", user_text)

                    # Turn complete — signal play_loop to stop speaking
                    if sc.turn_complete:
                        if self._turn_done_event:
                            self._turn_done_event.set()
                        if transcript:
                            full = re.sub(r"\s+", " ", " ".join(transcript)).strip()
                            if full and self._on_transcript:
                                self._on_transcript("aura", full)
                            transcript = []
                            logger.info("aura_turn_complete")

                # Tool calls
                if response.tool_call:
                    for call in response.tool_call.function_calls:
                        await self._handle_tool_call(session, call)

    # ── Tool call handler ─────────────────────────────────────────────────────

    async def _handle_tool_call(self, session: Any, call: Any) -> None:
        """Execute tool call and return result to Gemini session."""
        name = call.name
        args = dict(call.args) if call.args else {}

        logger.info("voice_tool_call", tool=name, args=list(args.keys()))
        if self._on_tool_call:
            self._on_tool_call(name, args)

        try:
            result = await self._executor.execute(name, args)
        except Exception as e:
            result = f"Tool error: {e}"

        logger.info("voice_tool_result", tool=name, result_len=len(result))

        try:
            from google.genai import types as gtypes  # type: ignore[import]
            await session.send_tool_response(
                function_responses=[
                    gtypes.FunctionResponse(
                        id=call.id,
                        name=name,
                        response={"result": result[:4000]},  # cap to avoid token waste
                    )
                ]
            )
        except Exception as e:
            logger.warning("tool_response_send_failed", tool=name, error=str(e))

    # ── Audio playback loop ───────────────────────────────────────────────────

    async def _play_loop(self) -> None:
        """Play received audio chunks through Mac speakers."""
        try:
            import sounddevice as sd  # type: ignore[import]
        except ImportError:
            logger.warning("sounddevice not installed — no audio playback")
            # Still drain the queue
            while self._running:
                if self._audio_in_queue:
                    try:
                        await asyncio.wait_for(self._audio_in_queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
            return

        import numpy as np  # type: ignore[import]
        logger.info("audio_play_loop_start")
        stream = sd.OutputStream(
            samplerate=_RECEIVE_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_CHUNK_SIZE,
        )
        stream.start()
        logger.info("audio_stream_started", samplerate=_RECEIVE_SAMPLE_RATE)
        chunks_played = 0
        try:
            while self._running:
                try:
                    chunk = await asyncio.wait_for(
                        self._audio_in_queue.get(), timeout=0.1
                    )
                    self.set_speaking(True)
                    pcm = np.frombuffer(chunk, dtype=np.int16)
                    await asyncio.to_thread(stream.write, pcm)
                    chunks_played += 1
                    if chunks_played == 1:
                        logger.info("audio_first_chunk_played", bytes=len(chunk))
                except asyncio.TimeoutError:
                    # Only stop speaking when Gemini signals turn_complete AND
                    # queue is drained — exact Mark-XXXIX pattern, prevents cutoff
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self._audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                        logger.debug("aura_playback_done")
        except Exception as e:
            logger.error("audio_playback_error", error=str(e))
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Auto-sleep loop ───────────────────────────────────────────────────────

    async def _auto_sleep_loop(self) -> None:
        """Go to sleep automatically after idle timeout."""
        while self._running:
            await asyncio.sleep(30)
            with self._sleep_lock:
                already_sleeping = self._sleeping
            if already_sleeping:
                continue
            idle = time.monotonic() - self._last_user_activity
            if idle >= self._auto_sleep_secs:
                logger.info("aura_auto_sleep", idle_secs=int(idle))
                self.sleep()

    # ── Microphone capture loop ───────────────────────────────────────────────

    def set_speaking(self, val: bool) -> None:
        with self._speaking_lock:
            self._is_speaking = val

    async def _mic_loop(self, session: Any) -> None:
        """Mic capture — exact pattern from Mark-XXXIX _listen_audio()."""
        try:
            import sounddevice as sd  # type: ignore[import]
        except ImportError:
            logger.warning("sounddevice not installed — no mic input")
            return

        loop = asyncio.get_event_loop()

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            with self._sleep_lock:
                sleeping = self._sleeping
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if sleeping or jarvis_speaking or self._text_pending:
                return  # sleep mode or echo-cancel: don't send mic audio
            data = indata.tobytes()
            loop.call_soon_threadsafe(
                self._out_queue.put_nowait,
                {"data": data, "mime_type": "audio/pcm"},
            )

        logger.info("mic_active")
        try:
            with sd.InputStream(
                samplerate=_SEND_SAMPLE_RATE,
                channels=_CHANNELS,
                dtype="int16",
                blocksize=_CHUNK_SIZE,
                callback=callback,
            ):
                logger.info("mic_first_chunk_sent")
                while self._running:
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.error("mic_error", error=str(e))
            raise


# ── Convenience factory ───────────────────────────────────────────────────────

def create_agent(
    on_transcript: Optional[Callable[[str, str], None]] = None,
    on_tool_call: Optional[Callable[[str, dict], None]] = None,
) -> GeminiLiveAgent:
    """Create and configure a GeminiLiveAgent from environment."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")

    return GeminiLiveAgent(
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        on_transcript=on_transcript,
        on_tool_call=on_tool_call,
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        owner_chat_id=os.environ.get("TELEGRAM_OWNER_CHAT_ID",
                                     os.environ.get("NOTIFICATION_CHAT_IDS", "").split(",")[0]),
    )
