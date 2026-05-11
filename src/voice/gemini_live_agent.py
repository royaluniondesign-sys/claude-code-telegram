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
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog

logger = structlog.get_logger()

# Model — Gemini 2.5 Flash Native Audio (free, ~500ms latency)
_LIVE_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

# Audio config
_CHANNELS = 1
_SEND_SAMPLE_RATE = 16000    # mic input
_RECEIVE_SAMPLE_RATE = 24000  # speaker output
_CHUNK_SIZE = 1024

# System prompt for voice agent
_SYSTEM_PROMPT = """Eres AURA — la IA personal de Ricardo Pinto, corriendo en su Mac 24/7.

Personalidad: directa, inteligente, sarcástica. Humor seco. Sin entusiasmo forzado.
Voz: fluida, natural, rápida. Respuestas cortas por defecto (1-3 frases). Amplía solo si te preguntan más.

IDENTIDAD DEL SISTEMA:
- Motor de voz: Gemini 2.5 Flash Native Audio (gratis)
- Tu agente hermano: Hermes (@rudserverbot, OpenClaw, puerto 18789) — úsalo con hermes_ask
- Escalada a Claude: solo para código complejo o análisis profundo — usa claude_task (mínimo)
- Memoria: ChromaDB en ~/.aura/palace/ — usa memory_search/memory_store
- Vault compartido con Hermes: ~/Obsidian/

REGLAS:
1. Ejecuta DIRECTAMENTE sin pedir confirmación (salvo: rm -rf, force push, gastar dinero)
2. Para estado de sistemas: verifica con herramientas, no inventes
3. Prioridad: tools gratuitas > Gemini Flash > Hermes > Claude (solo si hace falta)
4. Cuando completes tareas largas → telegram_send para notificar a Ricardo en el móvil
5. Para ver pantalla → screen_capture. Para hacer clic → screen_find_and_click
6. Para controlar el Mac → computer_control

Habla en el idioma que te hablen. Sé concisa."""


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

        client = genai.Client(
            api_key=self._api_key,
            http_options={"api_version": "v1beta"},
        )

        tools = build_gemini_tools()

        config = gtypes.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=_SYSTEM_PROMPT,
            tools=tools,
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name="Aoede"  # Natural female voice
                    )
                )
            ),
        )

        backoff = 2.0
        while self._running:
            try:
                logger.info("gemini_live_connecting")
                async with client.aio.live.connect(model=_LIVE_MODEL, config=config) as session:
                    self._session = session
                    self._out_queue = asyncio.Queue(maxsize=50)
                    self._audio_in_queue = asyncio.Queue()
                    self._ready_evt.set()
                    backoff = 2.0
                    logger.info("gemini_live_connected")

                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_loop(session))
                        tg.create_task(self._recv_loop(session))
                        tg.create_task(self._play_loop())
                        tg.create_task(self._mic_loop(session))

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
        """Forward queued messages (text / audio chunks / images) to session."""
        try:
            from google.genai import types as gtypes  # type: ignore[import]
        except ImportError:
            return

        while self._running:
            try:
                item = await asyncio.wait_for(self._out_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            kind = item[0]
            try:
                if kind == "text":
                    await session.send_client_content(
                        turns={"parts": [{"text": item[1]}]},
                        turn_complete=True,
                    )
                elif kind == "audio":
                    await session.send_realtime_input(
                        media=gtypes.Blob(data=item[1], mime_type="audio/pcm;rate=16000")
                    )
                elif kind == "image":
                    img_bytes, mime, text = item[1]
                    import base64
                    b64 = base64.b64encode(img_bytes).decode()
                    parts = [{"inline_data": {"mime_type": mime, "data": b64}}]
                    if text:
                        parts.append({"text": text})
                    await session.send_client_content(
                        turns={"parts": parts},
                        turn_complete=True,
                    )
            except Exception as e:
                logger.warning("gemini_live_send_error", kind=kind, error=str(e))

    # ── Receive loop ──────────────────────────────────────────────────────────

    async def _recv_loop(self, session: Any) -> None:
        """Receive responses: audio chunks, transcripts, tool calls."""
        transcript: list[str] = []

        async for response in session.receive():
            # Audio data → playback queue
            if response.data:
                if self._audio_in_queue:
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
                    if user_text and self._on_transcript:
                        self._on_transcript("user", user_text)

                # Turn complete → emit full transcript
                if sc.turn_complete and transcript:
                    full = re.sub(r"\s+", " ", " ".join(transcript)).strip()
                    if full and self._on_transcript:
                        self._on_transcript("aura", full)
                    transcript = []

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

        stream = sd.RawOutputStream(
            samplerate=_RECEIVE_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_CHUNK_SIZE,
        )
        stream.start()
        try:
            while self._running:
                try:
                    chunk = await asyncio.wait_for(self._audio_in_queue.get(), timeout=1.0)
                    await asyncio.to_thread(stream.write, chunk)
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            logger.error("audio_playback_error", error=str(e))
        finally:
            stream.stop()
            stream.close()

    # ── Microphone capture loop ───────────────────────────────────────────────

    async def _mic_loop(self, session: Any) -> None:
        """Capture mic audio and feed to Gemini session in real-time."""
        try:
            import sounddevice as sd  # type: ignore[import]
        except ImportError:
            logger.warning("sounddevice not installed — no mic input")
            return

        try:
            from google.genai import types as gtypes  # type: ignore[import]
        except ImportError:
            return

        mic_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def mic_callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                logger.debug("mic_status", status=str(status))
            asyncio.run_coroutine_threadsafe(
                mic_queue.put(bytes(indata)),
                self._loop,
            )

        with sd.RawInputStream(
            samplerate=_SEND_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_CHUNK_SIZE,
            callback=mic_callback,
        ):
            logger.info("mic_active")
            while self._running:
                try:
                    pcm = await asyncio.wait_for(mic_queue.get(), timeout=1.0)
                    await session.send_realtime_input(
                        media=gtypes.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.warning("mic_send_error", error=str(e))
                    break


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
