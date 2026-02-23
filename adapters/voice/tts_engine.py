"""
adapters/voice/tts_engine.py — Multi-provider Text-to-Speech engine.

Supports:
  - OpenAI TTS (tts-1, tts-1-hd) — cloud, high quality
  - ElevenLabs — cloud, most natural
  - MiniMax TTS (T2A v2) — cloud, Chinese-optimized
  - Sherpa-ONNX / Piper — local, zero-cost, offline

Architecture:
  - Provider abstraction via TTSProvider base class
  - Automatic fallback: primary → secondary → local
  - Output: WAV/MP3/OGG files ready for channel delivery
  - Caching: hash-based dedup for repeated phrases

Usage:
  engine = get_tts_engine()
  result = await engine.synthesize("Hello world", voice="alloy")
  # result = {"ok": True, "file_path": "/tmp/tts_abc123.mp3", "duration_s": 1.2}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Cache directory ──────────────────────────────────────────────────────────
TTS_CACHE_DIR = os.path.join("memory", "tts_cache")
TTS_CACHE_MAX_MB = 200  # Max cache size in MB


# ── Base Provider ────────────────────────────────────────────────────────────

class TTSProvider(ABC):
    """Abstract TTS provider."""

    name: str = "base"
    supports_streaming: bool = False

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice: str = "",
        speed: float = 1.0,
        output_format: str = "mp3",
    ) -> dict:
        """Synthesize text to audio file.

        Returns:
            {"ok": True, "file_path": str, "duration_s": float, "provider": str}
            or {"ok": False, "error": str}
        """
        ...

    @abstractmethod
    def available_voices(self) -> list[dict]:
        """Return list of available voices."""
        ...

    def is_available(self) -> bool:
        """Check if this provider is configured and usable."""
        return True


# ── OpenAI TTS ───────────────────────────────────────────────────────────────

class OpenAITTS(TTSProvider):
    """OpenAI TTS API (tts-1, tts-1-hd)."""

    name = "openai"

    VOICES = ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova",
              "sage", "shimmer"]
    MODELS = ["tts-1", "tts-1-hd"]

    def __init__(self, api_key: str = "", model: str = "tts-1"):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def synthesize(self, text: str, voice: str = "alloy",
                         speed: float = 1.0,
                         output_format: str = "mp3") -> dict:
        if not self.api_key:
            return {"ok": False, "error": "OPENAI_API_KEY not set"}

        voice = voice if voice in self.VOICES else "alloy"
        fmt = output_format if output_format in ("mp3", "opus", "aac",
                                                  "flac", "wav") else "mp3"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": text[:4096],
                        "voice": voice,
                        "speed": max(0.25, min(4.0, speed)),
                        "response_format": fmt,
                    },
                )
                if resp.status_code != 200:
                    return {"ok": False,
                            "error": f"OpenAI TTS {resp.status_code}: "
                                     f"{resp.text[:200]}"}

                out_path = _cache_path(text, voice, self.name, fmt)
                with open(out_path, "wb") as f:
                    f.write(resp.content)

                duration = _estimate_duration(out_path)
                return {
                    "ok": True,
                    "file_path": out_path,
                    "duration_s": duration,
                    "provider": self.name,
                    "voice": voice,
                }
        except ImportError:
            return {"ok": False, "error": "httpx not installed"}
        except Exception as e:
            return {"ok": False, "error": f"OpenAI TTS error: {e}"}

    def available_voices(self) -> list[dict]:
        return [{"id": v, "name": v.title(), "provider": self.name}
                for v in self.VOICES]


# ── ElevenLabs TTS ───────────────────────────────────────────────────────────

class ElevenLabsTTS(TTSProvider):
    """ElevenLabs TTS API — most natural sounding."""

    name = "elevenlabs"
    supports_streaming = True

    DEFAULT_VOICES = {
        "rachel": "21m00Tcm4TlvDq8ikWAM",
        "adam": "pNInz6obpgDQGcFmaJgB",
        "sam": "yoZ06aMxZJJ28mfd3POQ",
        "josh": "TxGEqnHWrfWFTfGW9XjX",
        "bella": "EXAVITQu4vr4xnSDxMaL",
    }

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def synthesize(self, text: str, voice: str = "rachel",
                         speed: float = 1.0,
                         output_format: str = "mp3") -> dict:
        if not self.api_key:
            return {"ok": False, "error": "ELEVENLABS_API_KEY not set"}

        voice_id = self.DEFAULT_VOICES.get(voice, voice)

        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    headers={
                        "xi-api-key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text[:5000],
                        "model_id": "eleven_multilingual_v2",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                            "speed": max(0.5, min(2.0, speed)),
                        },
                    },
                )
                if resp.status_code != 200:
                    return {"ok": False,
                            "error": f"ElevenLabs {resp.status_code}: "
                                     f"{resp.text[:200]}"}

                out_path = _cache_path(text, voice, self.name, "mp3")
                with open(out_path, "wb") as f:
                    f.write(resp.content)

                duration = _estimate_duration(out_path)
                return {
                    "ok": True,
                    "file_path": out_path,
                    "duration_s": duration,
                    "provider": self.name,
                    "voice": voice,
                }
        except ImportError:
            return {"ok": False, "error": "httpx not installed"}
        except Exception as e:
            return {"ok": False, "error": f"ElevenLabs error: {e}"}

    def available_voices(self) -> list[dict]:
        return [{"id": k, "name": k.title(), "provider": self.name,
                 "voice_id": v}
                for k, v in self.DEFAULT_VOICES.items()]


# ── MiniMax TTS ──────────────────────────────────────────────────────────────

class MiniMaxTTS(TTSProvider):
    """MiniMax T2A v2 — optimized for Chinese + English."""

    name = "minimax"

    VOICES = [
        "male-qn-qingse",       # youthful male
        "female-shaonv",        # young girl
        "male-qn-jingying",     # elite young male
        "female-yujie",         # mature female
        "presenter_male",       # male presenter
        "presenter_female",     # female presenter
        "audiobook_male_1",     # audiobook male 1
        "audiobook_female_1",   # audiobook female 1
    ]

    def __init__(self, api_key: str = "", group_id: str = ""):
        self.api_key = api_key or os.environ.get("MINIMAX_API_KEY", "")
        self.group_id = group_id or os.environ.get("MINIMAX_GROUP_ID", "")

    def is_available(self) -> bool:
        return bool(self.api_key and self.group_id)

    async def synthesize(self, text: str, voice: str = "presenter_male",
                         speed: float = 1.0,
                         output_format: str = "mp3") -> dict:
        if not self.api_key or not self.group_id:
            return {"ok": False, "error": "MINIMAX_API_KEY or GROUP_ID not set"}

        voice = voice if voice in self.VOICES else "presenter_male"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"https://api.minimax.chat/v1/t2a_v2?"
                    f"GroupId={self.group_id}",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "speech-01-turbo",
                        "text": text[:10000],
                        "timber_weights": [
                            {"voice_id": voice, "weight": 1}
                        ],
                        "audio_setting": {
                            "sample_rate": 32000,
                            "bitrate": 128000,
                            "format": "mp3",
                            "speed": max(0.5, min(2.0, speed)),
                        },
                    },
                )
                if resp.status_code != 200:
                    return {"ok": False,
                            "error": f"MiniMax TTS {resp.status_code}: "
                                     f"{resp.text[:200]}"}

                data = resp.json()
                if data.get("base_resp", {}).get("status_code", 0) != 0:
                    return {"ok": False,
                            "error": f"MiniMax TTS: "
                                     f"{data.get('base_resp', {})}"}

                # Decode audio from response
                audio_hex = data.get("data", {}).get("audio", "")
                if not audio_hex:
                    # Alternate response format: audio_file with URL
                    audio_url = (data.get("data", {})
                                 .get("audio_file", {}).get("url", ""))
                    if audio_url:
                        audio_resp = await client.get(audio_url, timeout=30)
                        audio_bytes = audio_resp.content
                    else:
                        return {"ok": False,
                                "error": "MiniMax TTS: no audio in response"}
                else:
                    import base64
                    audio_bytes = base64.b64decode(audio_hex)

                out_path = _cache_path(text, voice, self.name, "mp3")
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)

                duration = _estimate_duration(out_path)
                return {
                    "ok": True,
                    "file_path": out_path,
                    "duration_s": duration,
                    "provider": self.name,
                    "voice": voice,
                }
        except ImportError:
            return {"ok": False, "error": "httpx not installed"}
        except Exception as e:
            return {"ok": False, "error": f"MiniMax TTS error: {e}"}

    def available_voices(self) -> list[dict]:
        return [{"id": v, "name": v, "provider": self.name}
                for v in self.VOICES]


# ── Local TTS (Sherpa-ONNX / Piper) ─────────────────────────────────────────

class LocalTTS(TTSProvider):
    """Local TTS via sherpa-onnx or piper-tts CLI — zero cost, offline."""

    name = "local"

    def __init__(self):
        self._piper_path = self._find_binary("piper")
        self._sherpa_path = self._find_binary("sherpa-onnx-offline-tts")

    def _find_binary(self, name: str) -> str:
        """Find a TTS binary in PATH or common locations."""
        import shutil
        path = shutil.which(name)
        if path:
            return path
        # Check common install locations
        for prefix in ["/usr/local/bin", "/opt/homebrew/bin",
                       os.path.expanduser("~/.local/bin")]:
            full = os.path.join(prefix, name)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return full
        return ""

    def is_available(self) -> bool:
        return bool(self._piper_path or self._sherpa_path)

    async def synthesize(self, text: str, voice: str = "",
                         speed: float = 1.0,
                         output_format: str = "wav") -> dict:
        if self._piper_path:
            return await self._synth_piper(text, voice, speed)
        elif self._sherpa_path:
            return await self._synth_sherpa(text, voice, speed)
        return {"ok": False, "error": "No local TTS binary found "
                "(install piper or sherpa-onnx)"}

    async def _synth_piper(self, text: str, voice: str,
                           speed: float) -> dict:
        """Synthesize via Piper TTS."""
        out_path = _cache_path(text, voice or "default", self.name, "wav")

        # Default voice model (en_US-lessac-medium)
        model = voice or "en_US-lessac-medium"
        model_dir = os.path.expanduser("~/.local/share/piper/voices")

        cmd = [
            self._piper_path,
            "--model", os.path.join(model_dir, f"{model}.onnx"),
            "--output_file", out_path,
            "--length_scale", str(round(1.0 / max(0.5, speed), 2)),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(text.encode()), timeout=30)

            if proc.returncode != 0:
                return {"ok": False,
                        "error": f"Piper error: {stderr.decode()[:300]}"}

            duration = _estimate_duration(out_path)
            return {
                "ok": True,
                "file_path": out_path,
                "duration_s": duration,
                "provider": "piper",
                "voice": model,
            }
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Piper TTS timed out (30s)"}
        except Exception as e:
            return {"ok": False, "error": f"Piper error: {e}"}

    async def _synth_sherpa(self, text: str, voice: str,
                            speed: float) -> dict:
        """Synthesize via Sherpa-ONNX."""
        out_path = _cache_path(text, voice or "default", self.name, "wav")

        # Resolve model directory from env or default location
        model_dir = os.environ.get(
            "SHERPA_ONNX_MODEL_DIR",
            os.path.join(os.path.dirname(__file__), "sherpa-onnx", "models"))

        # Find model files in model_dir
        onnx_file = ""
        tokens_file = ""
        data_dir = ""
        for f in os.listdir(model_dir) if os.path.isdir(model_dir) else []:
            if f.endswith(".onnx"):
                onnx_file = os.path.join(model_dir, f)
            elif f == "tokens.txt":
                tokens_file = os.path.join(model_dir, f)
            elif f == "espeak-ng-data":
                data_dir = os.path.join(model_dir, f)

        if not onnx_file:
            return {"ok": False,
                    "error": f"No .onnx model found in {model_dir}"}

        cmd = [
            self._sherpa_path,
            f"--vits-model={onnx_file}",
        ]
        if tokens_file:
            cmd.append(f"--vits-tokens={tokens_file}")
        if data_dir:
            cmd.append(f"--vits-data-dir={data_dir}")
        # vits-length-scale: larger = slower, so invert user speed
        length_scale = round(1.0 / max(0.5, min(2.0, speed)), 3)
        cmd += [
            f"--vits-length-scale={length_scale}",
            f"--output-filename={out_path}",
            text[:2000],
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30)

            if proc.returncode != 0:
                return {"ok": False,
                        "error": f"Sherpa error: {stderr.decode()[:300]}"}

            duration = _estimate_duration(out_path)
            return {
                "ok": True,
                "file_path": out_path,
                "duration_s": duration,
                "provider": "sherpa-onnx",
                "voice": voice or "default",
            }
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Sherpa TTS timed out (30s)"}
        except Exception as e:
            return {"ok": False, "error": f"Sherpa error: {e}"}

    def available_voices(self) -> list[dict]:
        voices = []
        if self._piper_path:
            voices.append({"id": "en_US-lessac-medium", "name": "Lessac (EN)",
                          "provider": "piper"})
        if self._sherpa_path:
            voices.append({"id": "default", "name": "Default",
                          "provider": "sherpa-onnx"})
        return voices


# ── Unified TTS Engine ───────────────────────────────────────────────────────

class TTSEngine:
    """Multi-provider TTS engine with automatic fallback.

    Provider priority:
      1. Preferred provider (if set and available)
      2. MiniMax (if using MiniMax LLM already)
      3. OpenAI TTS
      4. ElevenLabs
      5. Local (Piper / Sherpa-ONNX)
    """

    def __init__(self, preferred_provider: str = ""):
        self.providers: dict[str, TTSProvider] = {}
        self._preferred = preferred_provider
        self._init_providers()

    def _init_providers(self):
        """Initialize all available providers."""
        for cls in [OpenAITTS, ElevenLabsTTS, MiniMaxTTS, LocalTTS]:
            try:
                provider = cls()
                self.providers[provider.name] = provider
            except Exception as e:
                logger.debug("TTS provider %s init failed: %s",
                             cls.name, e)

    def _provider_order(self) -> list[TTSProvider]:
        """Get providers in priority order."""
        order = []

        # Preferred first
        if self._preferred and self._preferred in self.providers:
            p = self.providers[self._preferred]
            if p.is_available():
                order.append(p)

        # Auto-detect: if MINIMAX keys set, prefer MiniMax
        prio = ["minimax", "openai", "elevenlabs", "local"]
        for name in prio:
            if name in self.providers and self.providers[name] not in order:
                if self.providers[name].is_available():
                    order.append(self.providers[name])

        return order

    async def synthesize(
        self,
        text: str,
        voice: str = "",
        speed: float = 1.0,
        output_format: str = "mp3",
        provider: str = "",
    ) -> dict:
        """Synthesize text to audio with automatic fallback.

        Args:
            text: Text to synthesize (max ~10000 chars)
            voice: Voice ID (provider-specific)
            speed: Speed multiplier (0.5-2.0)
            output_format: Output format (mp3, wav, ogg)
            provider: Force specific provider

        Returns:
            {"ok": True, "file_path": str, "duration_s": float, ...}
        """
        if not text.strip():
            return {"ok": False, "error": "Empty text"}

        # Check cache first
        cached = _check_cache(text, voice or "default", provider or "any",
                              output_format)
        if cached:
            return cached

        # Use specific provider if requested
        if provider and provider in self.providers:
            p = self.providers[provider]
            if p.is_available():
                result = await p.synthesize(text, voice, speed, output_format)
                if result.get("ok"):
                    return result
                logger.warning("[tts] %s failed: %s", provider,
                               result.get("error"))

        # Fallback chain
        providers = self._provider_order()
        if not providers:
            return {"ok": False,
                    "error": "No TTS providers available. Set one of: "
                    "OPENAI_API_KEY, ELEVENLABS_API_KEY, MINIMAX_API_KEY, "
                    "or install piper/sherpa-onnx locally."}

        errors = []
        for p in providers:
            result = await p.synthesize(text, voice, speed, output_format)
            if result.get("ok"):
                logger.info("[tts] Synthesized %d chars via %s (%.1fs)",
                            len(text), p.name, result.get("duration_s", 0))
                return result
            errors.append(f"{p.name}: {result.get('error', '?')}")

        return {"ok": False,
                "error": f"All TTS providers failed: {'; '.join(errors)}"}

    def list_voices(self, provider: str = "") -> list[dict]:
        """List all available voices across providers."""
        voices = []
        for name, p in self.providers.items():
            if provider and name != provider:
                continue
            if p.is_available():
                voices.extend(p.available_voices())
        return voices

    def list_providers(self) -> list[dict]:
        """List all providers with availability status."""
        return [
            {
                "name": p.name,
                "available": p.is_available(),
                "streaming": p.supports_streaming,
            }
            for p in self.providers.values()
        ]


# ── Cache Helpers ────────────────────────────────────────────────────────────

def _cache_path(text: str, voice: str, provider: str, fmt: str) -> str:
    """Generate a deterministic cache path for a TTS request."""
    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    key = f"{provider}:{voice}:{text}"
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return os.path.join(TTS_CACHE_DIR, f"tts_{h}.{fmt}")


def _check_cache(text: str, voice: str, provider: str, fmt: str) -> dict | None:
    """Check if a cached audio file exists and is fresh (< 24h)."""
    path = _cache_path(text, voice, provider, fmt)
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < 86400 and os.path.getsize(path) > 100:
            return {
                "ok": True,
                "file_path": path,
                "duration_s": _estimate_duration(path),
                "provider": provider,
                "voice": voice,
                "cached": True,
            }
    return None


def _estimate_duration(file_path: str) -> float:
    """Estimate audio duration from file size (rough)."""
    try:
        size = os.path.getsize(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".mp3":
            # ~16KB/s at 128kbps
            return round(size / 16000, 1)
        elif ext == ".wav":
            # ~32KB/s at 16kHz mono 16-bit
            return round(size / 32000, 1)
        elif ext in (".ogg", ".opus"):
            return round(size / 8000, 1)
        return round(size / 16000, 1)
    except Exception:
        return 0.0


def cleanup_cache(max_mb: int = TTS_CACHE_MAX_MB):
    """Remove old cache files if total exceeds max_mb."""
    if not os.path.isdir(TTS_CACHE_DIR):
        return
    files = []
    total = 0
    for f in os.listdir(TTS_CACHE_DIR):
        fp = os.path.join(TTS_CACHE_DIR, f)
        if os.path.isfile(fp):
            sz = os.path.getsize(fp)
            files.append((fp, os.path.getmtime(fp), sz))
            total += sz

    if total <= max_mb * 1024 * 1024:
        return

    # Remove oldest first
    files.sort(key=lambda x: x[1])
    for fp, _, sz in files:
        try:
            os.remove(fp)
            total -= sz
            if total <= max_mb * 1024 * 1024 * 0.8:
                break
        except OSError:
            pass


# ── Singleton ────────────────────────────────────────────────────────────────

_engine: TTSEngine | None = None


def get_tts_engine(preferred_provider: str = "") -> TTSEngine:
    """Get or create the singleton TTS engine."""
    global _engine
    if _engine is None:
        _engine = TTSEngine(preferred_provider=preferred_provider)
    return _engine
