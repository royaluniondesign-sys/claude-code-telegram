"""API Brain — zero-token, zero-LLM instant responses via free public APIs.

Handles specific intents by calling free no-auth APIs directly:
  - Weather: Open-Meteo (geocoding + forecast)
  - Crypto: CoinCap v2
  - Currency: fawazahmed0 CDN API
  - QR code: goqr.me (returns raw image bytes)
  - Dictionary: Free Dictionary API

All calls are async with a configurable timeout (default 8 seconds).
No LLM is invoked — pure HTTP → formatted string.
"""

import re
import time
import urllib.parse
from typing import Any, Dict, Optional

import aiohttp
import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_DEFAULT_TIMEOUT = 8

# ── Keyword patterns per intent ───────────────────────────────────────────────

_WEATHER_KW = re.compile(
    r"(?i)\b(clima|tiempo|weather|temperatura|llueve|calor|fr[íi]o|forecast|"
    r"temperature|rain|hot|cold)\b"
)

_CRYPTO_KW = re.compile(
    r"(?i)\b(bitcoin|btc|eth|ethereum|crypto|criptomoneda|precio\s+bitcoin|"
    r"cu[aá]nto\s+vale\s+(bitcoin|btc|eth|ethereum)|solana|sol|bnb|cardano|ada)\b"
)

_CURRENCY_KW = re.compile(
    r"(?i)\b(cambio|convertir|tipo\s+de\s+cambio|euros?\s+a|dollars?\s+a|"
    r"usd|eur|gbp|jpy|mxn|exchange\s+rate|€\s+to|\$\s+to|convert\b)\b"
)

_QR_KW = re.compile(r"(?i)\b(qr|c[oó]digo\s+qr|genera\s+qr|qrcode|make\s+qr|create\s+qr)\b")

_DICT_KW = re.compile(
    r"(?i)\b(qu[eé]\s+significa|define|meaning\s+of|definici[oó]n\s+de|"
    r"definition\s+of|what\s+does\s+\w+\s+mean)\b"
)

# ── Crypto slug mapping ───────────────────────────────────────────────────────

_CRYPTO_SLUGS: Dict[str, str] = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "bnb": "binance-coin",
    "cardano": "cardano",
    "ada": "cardano",
}

# ── Currency pair extraction ──────────────────────────────────────────────────

_CURRENCY_PAIR_RE = re.compile(
    r"(?i)([a-z]{3})\s+(?:a|to|en)\s+([a-z]{3})|"
    r"([€$£¥])\s+(?:a|to|en)\s+([€$£¥$a-z]{1,3})",
)
_SYMBOL_MAP = {"€": "eur", "$": "usd", "£": "gbp", "¥": "jpy"}


def _extract_currency_pair(prompt: str) -> tuple[str, str]:
    """Return (from_code, to_code) detected in prompt, or ("eur", "usd") default."""
    m = _CURRENCY_PAIR_RE.search(prompt)
    if m:
        if m.group(1) and m.group(2):
            return m.group(1).lower(), m.group(2).lower()
        sym_from = _SYMBOL_MAP.get(m.group(3), "eur")
        raw_to = m.group(4) or "usd"
        sym_to = _SYMBOL_MAP.get(raw_to, raw_to.lower())
        return sym_from, sym_to
    # Fallback: look for EUR/USD-style patterns
    pair = re.search(r"([a-z]{3})[/,\-]([a-z]{3})", prompt, re.IGNORECASE)
    if pair:
        return pair.group(1).lower(), pair.group(2).lower()
    return "eur", "usd"


def _extract_city(prompt: str) -> str:
    """Extract city name from prompt using common patterns."""
    # "clima en Madrid", "weather in New York", "temperatura de Londres"
    patterns = [
        r"(?:clima|tiempo|weather|temperatura|forecast)\s+(?:en|in|de|for)\s+([A-Za-záéíóúÁÉÍÓÚñÑüÜ\s]+?)(?:\?|$|,|\.|!)",
        r"(?:en|in)\s+([A-Za-záéíóúÁÉÍÓÚñÑüÜ\s]+?)(?:\?|$|,|\.|!)",
        r"([A-Z][a-záéíóúñü]{2,}(?:\s+[A-Z][a-záéíóúñü]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, prompt)
        if m:
            candidate = m.group(1).strip()
            if len(candidate) >= 3:
                return candidate
    return "Madrid"  # sane default


def _extract_word(prompt: str) -> str:
    """Extract word to define from prompt."""
    patterns = [
        r"(?:significa|define|definition\s+of|meaning\s+of|what\s+does\s+)\s+[\"']?(\w+)[\"']?",
        r"(?:definici[oó]n\s+de)\s+[\"']?(\w+)[\"']?",
        r"(?:define)\s+[\"']?(\w+)[\"']?",
    ]
    for pat in patterns:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # Last resort: last capitalized word or any word
    words = re.findall(r"\b[a-zA-Z]{3,}\b", prompt)
    # Filter common question words
    stop = {"what", "does", "mean", "significa", "define", "meaning", "the", "of", "que"}
    filtered = [w for w in words if w.lower() not in stop]
    return filtered[-1] if filtered else "serendipity"


def _extract_qr_data(prompt: str) -> str:
    """Extract the text/URL for QR generation."""
    # Try URL first
    url_m = re.search(r"https?://\S+", prompt)
    if url_m:
        return url_m.group(0)
    # Strip command words
    clean = re.sub(
        r"(?i)\b(genera|crea|make|create|generate|c[oó]digo\s+qr|qr|qrcode)\b",
        "",
        prompt,
    ).strip()
    return clean or "https://aura.ai"


def _wmo_description(code: int) -> str:
    """Map WMO weather code to a short emoji+description."""
    _MAP = {
        0: "☀️ despejado",
        1: "🌤 casi despejado",
        2: "⛅ parcialmente nublado",
        3: "☁️ cubierto",
        45: "🌫 niebla",
        48: "🌫 niebla con escarcha",
        51: "🌦 llovizna leve",
        61: "🌧 lluvia leve",
        63: "🌧 lluvia moderada",
        65: "🌧 lluvia intensa",
        71: "🌨 nieve leve",
        80: "🌦 chubascos",
        95: "⛈ tormenta",
    }
    for threshold in sorted(_MAP, reverse=True):
        if code >= threshold:
            return _MAP[threshold]
    return "🌡 desconocido"


class ApiBrain(Brain):
    """Zero-token brain: free no-auth public APIs for instant intent responses."""

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "api-zero"

    @property
    def display_name(self) -> str:
        return "API Zero-Token"

    @property
    def emoji(self) -> str:
        return "⚡"

    # ── Internal API callers ──────────────────────────────────────────────────

    async def _weather(self, prompt: str) -> BrainResponse:
        city = _extract_city(prompt)
        start = time.time()
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Step 1: geocoding
            geo_url = (
                f"https://geocoding-api.open-meteo.com/v1/search"
                f"?name={urllib.parse.quote(city)}&count=1&language=es&format=json"
            )
            async with session.get(geo_url) as r:
                if r.status != 200:
                    return self._error_response("geocoding API error", start)
                geo_data = await r.json()

            results = geo_data.get("results")
            if not results:
                return self._error_response(
                    f"Ciudad '{city}' no encontrada. Prueba con otra ciudad.", start
                )

            loc = results[0]
            lat = loc["latitude"]
            lon = loc["longitude"]
            city_name = loc.get("name", city)
            country = loc.get("country_code", "")

            # Step 2: forecast
            forecast_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,precipitation,windspeed_10m,weathercode"
                f"&timezone=auto"
            )
            async with session.get(forecast_url) as r:
                if r.status != 200:
                    return self._error_response("forecast API error", start)
                fc = await r.json()

        current = fc.get("current", {})
        temp = current.get("temperature_2m", "?")
        wind = current.get("windspeed_10m", "?")
        precip = current.get("precipitation", 0)
        wcode = int(current.get("weathercode", 0))
        desc = _wmo_description(wcode)

        rain_str = f"💧 {precip}mm" if precip and precip > 0 else "sin lluvia"
        loc_str = f"{city_name}"
        if country:
            loc_str += f" ({country.upper()})"

        content = f"{desc}\n{loc_str}: {temp}°C · viento {wind} km/h · {rain_str}"
        return BrainResponse(
            content=content,
            brain_name=self.name,
            duration_ms=int((time.time() - start) * 1000),
        )

    async def _crypto(self, prompt: str) -> BrainResponse:
        start = time.time()
        # Detect which crypto
        slug = "bitcoin"
        for kw, s in _CRYPTO_SLUGS.items():
            if re.search(rf"(?i)\b{kw}\b", prompt):
                slug = s
                break

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        url = f"https://api.coincap.io/v2/assets/{slug}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Accept": "application/json"}) as r:
                if r.status != 200:
                    return self._error_response(f"CoinCap API error {r.status}", start)
                data = await r.json()

        asset = data.get("data", {})
        name = asset.get("name", slug.title())
        symbol = asset.get("symbol", slug.upper())
        price_usd = float(asset.get("priceUsd", 0))
        change_24h = float(asset.get("changePercent24Hr", 0))
        sign = "+" if change_24h >= 0 else ""

        # Format price with commas
        if price_usd >= 1:
            price_str = f"${price_usd:,.2f}"
        else:
            price_str = f"${price_usd:.6f}"

        emoji = "₿" if symbol == "BTC" else "🪙"
        content = f"{emoji} {name} ({symbol}): {price_str} ({sign}{change_24h:.2f}% 24h)"
        return BrainResponse(
            content=content,
            brain_name=self.name,
            duration_ms=int((time.time() - start) * 1000),
        )

    async def _currency(self, prompt: str) -> BrainResponse:
        start = time.time()
        from_code, to_code = _extract_currency_pair(prompt)

        url = f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{from_code}.json"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as r:
                if r.status != 200:
                    return self._error_response(
                        f"Currency API error {r.status} for {from_code.upper()}", start
                    )
                data = await r.json(content_type=None)

        rates = data.get(from_code, {})
        rate = rates.get(to_code)
        if rate is None:
            return self._error_response(
                f"Par {from_code.upper()}/{to_code.upper()} no disponible.", start
            )

        content = f"💱 1 {from_code.upper()} = {rate:.4f} {to_code.upper()} (live)"
        return BrainResponse(
            content=content,
            brain_name=self.name,
            duration_ms=int((time.time() - start) * 1000),
        )

    async def _qr(self, prompt: str) -> BrainResponse:
        """Generate QR and return raw bytes encoded in the content field."""
        start = time.time()
        data_str = _extract_qr_data(prompt)
        encoded = urllib.parse.quote(data_str, safe="")
        url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded}"

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as r:
                if r.status != 200:
                    return self._error_response(f"QR API error {r.status}", start)
                image_bytes = await r.read()

        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        content = f"__QR_IMAGE_B64__:{b64}"
        return BrainResponse(
            content=content,
            brain_name=self.name,
            duration_ms=int((time.time() - start) * 1000),
            metadata={"qr_for": data_str, "image_bytes_len": len(image_bytes)},
        )

    async def _dictionary(self, prompt: str) -> BrainResponse:
        start = time.time()
        word = _extract_word(prompt)
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as r:
                if r.status == 404:
                    return self._error_response(
                        f"'{word}' not found in dictionary.", start
                    )
                if r.status != 200:
                    return self._error_response(f"Dictionary API error {r.status}", start)
                entries = await r.json()

        if not entries or not isinstance(entries, list):
            return self._error_response(f"No results for '{word}'.", start)

        entry = entries[0]
        phonetic = entry.get("phonetic", "")
        meanings = entry.get("meanings", [])
        if not meanings:
            return self._error_response(f"No definitions found for '{word}'.", start)

        parts = [f"📖 *{word}*" + (f" /{phonetic}/" if phonetic else "")]
        for meaning in meanings[:2]:
            pos = meaning.get("partOfSpeech", "")
            defs = meaning.get("definitions", [])
            if defs:
                defn = defs[0].get("definition", "")
                example = defs[0].get("example", "")
                parts.append(f"_{pos}_: {defn}")
                if example:
                    parts.append(f'  e.g. "{example}"')

        content = "\n".join(parts)
        return BrainResponse(
            content=content,
            brain_name=self.name,
            duration_ms=int((time.time() - start) * 1000),
        )

    def _error_response(self, message: str, start: float) -> BrainResponse:
        return BrainResponse(
            content=f"⚡ API error: {message}",
            brain_name=self.name,
            duration_ms=int((time.time() - start) * 1000),
            is_error=True,
            error_type="api_error",
        )

    def _classify_intent(self, prompt: str) -> Optional[str]:
        """Return intent key or None if this brain can't handle the prompt."""
        if _QR_KW.search(prompt):
            return "qr"
        if _DICT_KW.search(prompt):
            return "dictionary"
        if _CRYPTO_KW.search(prompt):
            return "crypto"
        if _CURRENCY_KW.search(prompt):
            return "currency"
        if _WEATHER_KW.search(prompt):
            return "weather"
        return None

    # ── Brain interface ───────────────────────────────────────────────────────

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        **_: Any,
    ) -> BrainResponse:
        """Parse intent from prompt and call the appropriate free API."""
        intent = self._classify_intent(prompt)
        if intent is None:
            return BrainResponse(
                content="No reconozco esta consulta para API Zero-Token.",
                brain_name=self.name,
                is_error=True,
                error_type="unrecognized_intent",
            )

        try:
            if intent == "weather":
                return await self._weather(prompt)
            if intent == "crypto":
                return await self._crypto(prompt)
            if intent == "currency":
                return await self._currency(prompt)
            if intent == "qr":
                return await self._qr(prompt)
            if intent == "dictionary":
                return await self._dictionary(prompt)
        except aiohttp.ClientConnectorError as exc:
            logger.warning("api_brain_connect_error", intent=intent, error=str(exc))
            return BrainResponse(
                content=f"⚡ Sin conexión a la API de {intent}.",
                brain_name=self.name,
                is_error=True,
                error_type="connection_error",
            )
        except TimeoutError:
            logger.warning("api_brain_timeout", intent=intent, timeout=self._timeout)
            return BrainResponse(
                content=f"⚡ Timeout ({self._timeout}s) — API de {intent} no respondió.",
                brain_name=self.name,
                is_error=True,
                error_type="timeout",
            )
        except Exception as exc:
            logger.error("api_brain_error", intent=intent, error=str(exc))
            return BrainResponse(
                content=f"⚡ Error inesperado ({intent}): {exc}",
                brain_name=self.name,
                is_error=True,
                error_type=type(exc).__name__,
            )

        # Should not reach here
        return BrainResponse(
            content="Intent desconocido.",
            brain_name=self.name,
            is_error=True,
            error_type="unknown_intent",
        )

    async def health_check(self) -> BrainStatus:
        """Check Open-Meteo reachability (lightweight ping)."""
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://geocoding-api.open-meteo.com/v1/search?name=Madrid&count=1"
                ) as r:
                    if r.status == 200:
                        return BrainStatus.READY
                    return BrainStatus.ERROR
        except Exception as exc:
            logger.debug("api_brain_health_error", error=str(exc))
            return BrainStatus.ERROR

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "auth": "None (all APIs are free, no key required)",
            "cost": "FREE — zero tokens",
            "apis": [
                "Open-Meteo (weather + geocoding)",
                "CoinCap v2 (crypto prices)",
                "fawazahmed0 CDN (currency exchange)",
                "goqr.me (QR code generation)",
                "Free Dictionary API (English definitions)",
            ],
            "timeout_seconds": self._timeout,
        }
