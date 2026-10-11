"""
TTS Handler — Stage 2: Text-to-Speech.

Kiến trúc:
  - BaseTTSHandler: abstract interface
  - ElevenLabsTTSHandler: cloud TTS với emotion → VoiceSettings mapping
  - GoogleCloudTTSHandler: GCP TTS với LINEAR16 WAV format
  - CoquiXTTSHandler: local voice cloning với XTTS-v2 (chỉ cần 6-10s mẫu giọng)
  - NoOpTTSHandler: fallback khi không cấu hình TTS (trả về bytes rỗng)

Factory function `get_tts_handler()` hỗ trợ cấu hình qua tts_provider hoặc tự động fallback: ElevenLabs → XTTS → NoOp.
"""

import asyncio
import base64
import io
import logging
import struct
import wave
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from importlib import import_module

from app.config import settings
from app.models import Emotion, SentenceChunk

logger = logging.getLogger(__name__)

try:
    from langsmith import traceable
except ImportError:
    # Safe fallback if langsmith is not installed
    def traceable(*args, **kwargs):
         return lambda func: func


try:
    from elevenlabs import VoiceSettings
    from elevenlabs.client import AsyncElevenLabs
except Exception as exc:  # pragma: no cover - depends on optional SDK packaging
    logger.warning("ElevenLabs SDK import failed; cloud TTS disabled: %s", exc)
    AsyncElevenLabs = None  # type: ignore[assignment]

    class VoiceSettings:  # type: ignore[no-redef]
        def __init__(
            self,
            stability: float,
            similarity_boost: float,
            style: float,
            use_speaker_boost: bool,
        ) -> None:
            self.stability = stability
            self.similarity_boost = similarity_boost
            self.style = style
            self.use_speaker_boost = use_speaker_boost

try:
    from google.cloud import texttospeech
except Exception as exc:
    logger.warning("Google Cloud Text-to-Speech SDK import failed: %s", exc)
    texttospeech = None

# ---------------------------------------------------------------------------
# Emotion → VoiceSettings mapping
# Tham số: stability (0-1, cao = ít biến tấu), similarity_boost (0-1),
#          style (0-1, cao = phóng đại biểu cảm), use_speaker_boost
# ---------------------------------------------------------------------------
_EMOTION_VOICE_SETTINGS: dict[Emotion, VoiceSettings] = {
    Emotion.joy: VoiceSettings(
        stability=0.30,
        similarity_boost=0.75,
        style=0.55,
        use_speaker_boost=True,
    ),
    Emotion.sad: VoiceSettings(
        stability=0.85,
        similarity_boost=0.70,
        style=0.10,
        use_speaker_boost=False,
    ),
    Emotion.neutral: VoiceSettings(
        stability=0.50,
        similarity_boost=0.75,
        style=0.00,
        use_speaker_boost=True,
    ),
    Emotion.thinking: VoiceSettings(
        stability=0.70,
        similarity_boost=0.70,
        style=0.10,
        use_speaker_boost=False,
    ),
    Emotion.surprise: VoiceSettings(
        stability=0.20,
        similarity_boost=0.80,
        style=0.70,
        use_speaker_boost=True,
    ),
    Emotion.anger: VoiceSettings(
        stability=0.30,
        similarity_boost=0.90,
        style=0.80,
        use_speaker_boost=True,
    ),
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseTTSHandler(ABC):
    @property
    def provider_name(self) -> str:
        return "unknown"

    @abstractmethod
    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        """Chuyển SentenceChunk → audio bytes. Trả về b'' nếu không có audio."""

    async def warmup(self) -> None:
        """Pre-load model & GPU vào VRAM. Override ở subclass nếu cần."""

    @property
    def is_active(self) -> bool:
        """True nếu handler có thể sinh audio thật."""
        return True


# ---------------------------------------------------------------------------
# ElevenLabs implementation
# ---------------------------------------------------------------------------

class ElevenLabsTTSHandler(BaseTTSHandler):
    @property
    def provider_name(self) -> str:
        return "elevenlabs"

    def __init__(self) -> None:
        if AsyncElevenLabs is None:
            raise RuntimeError("ElevenLabs SDK is unavailable.")
        self._client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
        self._voice_id = settings.elevenlabs_voice_id
        self._model_id = settings.elevenlabs_model_id
        self._output_format = settings.elevenlabs_output_format

    @traceable(name="ElevenLabs TTS Synthesize", run_type="tool")
    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        """
        Gọi ElevenLabs convert() để stream audio bytes về, gom lại thành
        một buffer hoàn chỉnh cho câu hiện tại.
        Latency thực tế: ~200-400ms cho câu ngắn với eleven_turbo_v2_5.
        """
        voice_settings = _EMOTION_VOICE_SETTINGS.get(chunk.emotion, _EMOTION_VOICE_SETTINGS[Emotion.neutral])

        logger.debug(
            "TTS synthesize | emotion=%s stability=%.2f style=%.2f | text=%r",
            chunk.emotion,
            voice_settings.stability,
            voice_settings.style or 0.0,
            chunk.text[:60],
        )

        audio_buf = bytearray()
        try:
            async for audio_chunk in self._client.text_to_speech.convert(
                voice_id=self._voice_id,
                text=chunk.text,
                model_id=self._model_id,
                output_format=self._output_format,  # type: ignore[arg-type]
                voice_settings=voice_settings,
            ):
                audio_buf.extend(audio_chunk)

            logger.debug("TTS done | %d bytes for %r", len(audio_buf), chunk.text[:40])
            return bytes(audio_buf)

        except Exception as exc:
            logger.error("ElevenLabs TTS error: %s", exc)
            raise


# ---------------------------------------------------------------------------
# Google Cloud TTS implementation
# ---------------------------------------------------------------------------

class GoogleCloudTTSHandler(BaseTTSHandler):
    """
    Google Cloud Text-to-Speech service.
    Generates audio in LINEAR16 format (WAV format with header) which works natively with Rhubarb.
    """

    def __init__(self) -> None:
        if texttospeech is None:
            raise RuntimeError("Google Cloud Text-to-Speech SDK is unavailable.")
        self._client = texttospeech.TextToSpeechAsyncClient()
        self._voice_name = settings.google_tts_voice_name
        self._language_code = settings.google_tts_language_code

    @property
    def provider_name(self) -> str:
        return "google-cloud"

    @traceable(name="Google Cloud TTS Synthesize", run_type="tool")
    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        logger.debug(
            "Google Cloud TTS synthesize | text=%r",
            chunk.text[:60],
        )
        try:
            synthesis_input = texttospeech.SynthesisInput(text=chunk.text)
            voice = texttospeech.VoiceSelectionParams(
                language_code=self._language_code,
                name=self._voice_name,
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16
            )
            response = await self._client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config,
            )
            return response.audio_content
        except Exception as exc:
            logger.error("Google Cloud TTS error: %s", exc)
            raise


# ---------------------------------------------------------------------------
# Custom GCP TTS implementation (deployed model)
# ---------------------------------------------------------------------------

class GCPCustomTTSHandler(BaseTTSHandler):
    """
    Handler for a custom TTS model hosted on GCP (Cloud Run, Vertex AI, GKE).
    Sends the text payload via HTTP POST and expects audio bytes in return.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: float = 10.0) -> None:
        self._url = settings.gcp_custom_tts_url if url is None else url
        self._api_key = settings.gcp_custom_tts_api_key if api_key is None else api_key
        self._timeout = timeout
        if not self._url:
            raise RuntimeError("GCP_CUSTOM_TTS_URL is not configured.")

    @property
    def provider_name(self) -> str:
        return "gcp-custom"

    @traceable(name="GCP Custom TTS Synthesize", run_type="tool")
    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        logger.debug(

            "Custom GCP TTS synthesize | url=%s | text=%r",
            self._url,
            chunk.text[:60],
        )
        
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "text": chunk.text,
            "voice": chunk.voice if hasattr(chunk, "voice") and chunk.voice else "default",
            "emotion": chunk.emotion.value if hasattr(chunk, "emotion") and chunk.emotion else "neutral",
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=payload, headers=headers)
                if response.status_code != 200:
                    logger.error(
                        "Custom GCP TTS server returned status %d: %s",
                        response.status_code,
                        response.text,
                    )
                    response.raise_for_status()
                return response.content
        except Exception as exc:
            logger.error("Custom GCP TTS request failed: %s", exc)
            raise

    async def warmup(self) -> None:
        logger.info("Warming up Custom GCP TTS endpoint: %s", self._url)
        dummy = SentenceChunk(text="Hi.", emotion=Emotion.neutral)
        try:
            await self.synthesize(dummy)
            logger.info("Custom GCP TTS endpoint warmed up successfully.")
        except Exception as exc:
            logger.warning("Custom GCP TTS warmup failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
class KokoroTTSHandler(GCPCustomTTSHandler):
    """Use the local CPU speech service with the existing audio contract."""

    def __init__(self) -> None:
        super().__init__(url=settings.kokoro_tts_url, api_key="",
                         timeout=settings.kokoro_tts_timeout_seconds)

    @property
    def provider_name(self) -> str:
        return "kokoro"

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(self._url.rsplit("/", 1)[0] + "/health")
                return response.status_code == 200 and response.json().get("model_loaded") is True
        except Exception:
            return False

    async def warmup(self) -> None:
        await self.synthesize(SentenceChunk(text="Hello.", emotion=Emotion.neutral))


class ChatterboxNanoTTSHandler(KokoroTTSHandler):
    """Clone uploaded voices; use Kokoro presets when no reference or on failure."""

    def __init__(self) -> None:
        GCPCustomTTSHandler.__init__(self, url=settings.chatterbox_tts_url, api_key="",
                                    timeout=settings.chatterbox_tts_timeout_seconds)
        self._fallback = KokoroTTSHandler()

    @property
    def provider_name(self) -> str:
        return "chatterbox-nano+kokoro"

    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        if chunk.voice and "/" in chunk.voice:
            try:
                return await GCPCustomTTSHandler.synthesize(self, chunk)
            except Exception:
                logger.warning("Nano unavailable; using Kokoro for this sentence.")
            chunk = chunk.model_copy(update={"voice": "af_bella"})
        return await self._fallback.synthesize(chunk)

    async def health_check(self) -> bool:
        return await super().health_check() or await self._fallback.health_check()

    async def warmup(self) -> None:
        await self._fallback.warmup()


# Coqui XTTS-v2: local voice cloning
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_xtts_model(model_name: str):
    """Load XTTS model một lần duy nhất, cache lại để dùng lại."""
    # Avoid Coqui's interactive license prompt in local/dev server processes.
    import os
    os.environ.setdefault("COQUI_TOS_AGREED", "1")

    try:
        torch = import_module("torch")
        logger.info("PyTorch %s | CUDA available: %s", torch.__version__, torch.cuda.is_available())
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch chưa được cài. Chạy:\n"
            "pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121"
        ) from exc

    try:
        import_module("torchaudio")
        logger.info("torchaudio OK")
    except ImportError as exc:
        raise RuntimeError(
            "torchaudio chưa được cài. Chạy:\n"
            "pip install torchaudio --index-url https://download.pytorch.org/whl/cu121"
        ) from exc

    try:
        from TTS.api import TTS  # type: ignore[import]
        logger.info("TTS import OK")
    except Exception as exc:
        logger.error("TTS import failed: %s", exc, exc_info=True)
        raise RuntimeError(f"Không thể import TTS: {exc}") from exc

    logger.info("Loading XTTS model '%s' — lần đầu sẽ tải về (~2GB)...", model_name)
    use_gpu = torch.cuda.is_available()
    logger.info("XTTS using %s", "GPU" if use_gpu else "CPU")
    tts = TTS(model_name, gpu=use_gpu)
    logger.info("XTTS model loaded.")
    return tts


class CoquiXTTSHandler(BaseTTSHandler):
    """
    Voice cloning local với XTTS-v2.
    Cần file giọng mẫu WAV (6-10 giây, mono/stereo, 22050Hz+).
    Model được load LAZY — lần đầu synthesize mới load, server start bình thường.
    Chạy inference trong thread pool để không block event loop.
    """

    def __init__(self, speaker_wav: str, language: str, model_name: str) -> None:
        self._speaker_wav = speaker_wav
        self._language = language
        self._model_name = model_name
        self._tts = None          # lazy — chưa load lúc khởi tạo
        # max_workers=1: XTTS model không thread-safe, chỉ cho phép 1 synthesis tại một thời điểm
        self._executor = ThreadPoolExecutor(max_workers=1)

    @property
    def provider_name(self) -> str:
        return "xtts"

    def _get_tts(self):
        """Load model lần đầu tiên khi cần, cache lại cho các lần sau."""
        if self._tts is None:
            self._tts = _load_xtts_model(self._model_name)
        return self._tts

    @traceable(name="Coqui XTTS Synthesize", run_type="tool")
    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        loop = asyncio.get_event_loop()


        def _run() -> bytes:
            try:
                import soundfile as sf  # type: ignore[import]
            except ImportError as exc:
                raise RuntimeError("Thiếu soundfile. Chạy: pip install soundfile") from exc

            import os
            speaker_wav = self._speaker_wav
            if hasattr(chunk, "voice") and chunk.voice:
                if settings.gcs_bucket_name:
                    local_cache_path = os.path.join("voices", chunk.voice)
                    if os.path.isfile(local_cache_path):
                        speaker_wav = local_cache_path
                    else:
                        try:
                            from google.cloud import storage
                            storage_client = storage.Client()
                            bucket = storage_client.bucket(settings.gcs_bucket_name)
                            blob = bucket.blob(f"voices/{chunk.voice}")
                            if blob.exists():
                                os.makedirs(os.path.dirname(local_cache_path), exist_ok=True)
                                blob.download_to_filename(local_cache_path)
                                logger.info("Downloaded GCS voice %s to %s", chunk.voice, local_cache_path)
                                speaker_wav = local_cache_path
                        except Exception as exc:
                            logger.error("Failed to download GCS voice %s: %s", chunk.voice, exc)
                else:
                    dynamic_voice_path = os.path.join("voices", chunk.voice)
                    if os.path.isfile(dynamic_voice_path):
                        speaker_wav = dynamic_voice_path

            logger.debug(
                "XTTS synthesize | lang=%s | text=%r | voice=%s",
                self._language,
                chunk.text[:60],
                speaker_wav,
            )

            wav: list[float] = self._get_tts().tts(
                text=chunk.text,
                speaker_wav=speaker_wav,
                language=self._language,
            )

            buf = io.BytesIO()
            sf.write(buf, wav, samplerate=24000, format="WAV")
            buf.seek(0)
            return buf.read()

        audio_bytes = await loop.run_in_executor(self._executor, _run)
        logger.debug("XTTS done | %d bytes", len(audio_bytes))
        return audio_bytes

    async def warmup(self) -> None:
        """
        Pre-load XTTS model + warm-up GPU ngay lúc server start.
        Synthesize một câu ngắn để:
          - Load model vào VRAM (tránh delay ở request đầu tiên)
          - Khởi tạo CUDA kernels
          - Cache speaker embedding từ file WAV mẫu
        """
        logger.info("XTTS: warming up model (pre-loading into GPU)...")
        dummy = SentenceChunk(text="Hello.", emotion=Emotion.neutral)
        try:
            await self.synthesize(dummy)
            logger.info("XTTS: warmup complete — model ready in VRAM.")
        except Exception as exc:
            logger.warning("XTTS warmup failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Fallback: no-op (TTS không cấu hình)
# ---------------------------------------------------------------------------

class NoOpTTSHandler(BaseTTSHandler):
    """
    Trả về bytes rỗng. Frontend sẽ nhận AudioChunkPayload với audio_base64=""
    và fallback về hiển thị text thay vì phát audio.
    """

    @property
    def is_active(self) -> bool:
        return False

    @property
    def provider_name(self) -> str:
        return "none"

    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        logger.debug("NoOpTTS: no audio for %r", chunk.text[:40])
        return b""


class FallbackTTSHandler(BaseTTSHandler):
    """Ưu tiên primary, tự động fallback sang secondary khi primary lỗi."""

    def __init__(self, primary: BaseTTSHandler, secondary: BaseTTSHandler) -> None:
        self._primary = primary
        self._secondary = secondary

    @property
    def is_active(self) -> bool:
        return self._primary.is_active or self._secondary.is_active

    @property
    def provider_name(self) -> str:
        return f"{self._primary.provider_name}+fallback:{self._secondary.provider_name}"

    async def synthesize(self, chunk: SentenceChunk) -> bytes:
        try:
            return await self._primary.synthesize(chunk)
        except Exception as exc:
            logger.warning(
                "Primary TTS provider '%s' failed; retrying with '%s': %s",
                self._primary.provider_name,
                self._secondary.provider_name,
                exc,
            )
            return await self._secondary.synthesize(chunk)

    async def warmup(self) -> None:
        try:
            await self._primary.warmup()
        except Exception as exc:
            logger.warning("Primary TTS warmup failed (non-fatal): %s", exc)

        try:
            await self._secondary.warmup()
        except Exception as exc:
            logger.warning("Fallback TTS warmup failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_tts_handler() -> BaseTTSHandler:
    provider = settings.tts_provider.lower().strip()
    if provider == "chatterbox-nano":
        return ChatterboxNanoTTSHandler()
    elif provider == "kokoro":
        return KokoroTTSHandler()
    elif provider == "google-cloud":
        logger.info("TTS: Google Cloud Text-to-Speech (voice=%s)", settings.google_tts_voice_name)
        return GoogleCloudTTSHandler()
    elif provider == "gcp-custom":
        logger.info("TTS: Custom GCP TTS (endpoint=%s)", settings.gcp_custom_tts_url)
        return GCPCustomTTSHandler()
    elif provider == "elevenlabs":
        logger.info("TTS: ElevenLabs (voice=%s, model=%s)", settings.elevenlabs_voice_id, settings.elevenlabs_model_id)
        return ElevenLabsTTSHandler()
    elif provider == "xtts":
        if settings.xtts_speaker_wav:
            import os
            if os.path.isfile(settings.xtts_speaker_wav):
                logger.info(
                    "TTS: Coqui XTTS-v2 | speaker_wav=%s | language=%s",
                    settings.xtts_speaker_wav,
                    settings.xtts_language,
                )
                return CoquiXTTSHandler(
                    speaker_wav=settings.xtts_speaker_wav,
                    language=settings.xtts_language,
                    model_name=settings.xtts_model_name,
                )
        logger.error("XTTS selected but speaker WAV file not configured or not found. Running text-only.")
        return NoOpTTSHandler()

    xtts_handler: BaseTTSHandler | None = None
    if settings.xtts_speaker_wav:
        import os
        if not os.path.isfile(settings.xtts_speaker_wav):
            logger.error(
                "XTTS_SPEAKER_WAV '%s' không tồn tại — local XTTS disabled.",
                settings.xtts_speaker_wav,
            )
        else:
            logger.info(
                "TTS: Coqui XTTS-v2 | speaker_wav=%s | language=%s",
                settings.xtts_speaker_wav,
                settings.xtts_language,
            )
            xtts_handler = CoquiXTTSHandler(
                speaker_wav=settings.xtts_speaker_wav,
                language=settings.xtts_language,
                model_name=settings.xtts_model_name,
            )

    if settings.gcp_custom_tts_url:
        logger.info("TTS: Custom GCP TTS (endpoint=%s)", settings.gcp_custom_tts_url)
        gcp_custom_handler = GCPCustomTTSHandler()
        if xtts_handler is not None:
            logger.info("TTS: local XTTS fallback is enabled if Custom GCP TTS fails.")
            return FallbackTTSHandler(primary=gcp_custom_handler, secondary=xtts_handler)
        return gcp_custom_handler

    if settings.elevenlabs_api_key:
        if AsyncElevenLabs is None:
            logger.error("ELEVENLABS_API_KEY is set, but ElevenLabs SDK is unavailable.")
            if xtts_handler is not None:
                logger.info("TTS: falling back to local XTTS because ElevenLabs SDK is unavailable.")
                return xtts_handler
            logger.error("No local XTTS fallback available; running text-only.")
            return NoOpTTSHandler()

        logger.info("TTS: ElevenLabs (voice=%s, model=%s)", settings.elevenlabs_voice_id, settings.elevenlabs_model_id)
        elevenlabs_handler = ElevenLabsTTSHandler()
        if xtts_handler is not None:
            logger.info("TTS: local XTTS fallback is enabled if ElevenLabs fails.")
            return FallbackTTSHandler(primary=elevenlabs_handler, secondary=xtts_handler)
        return elevenlabs_handler

    if xtts_handler is not None:
        return xtts_handler

    logger.warning("Không có TTS nào được cấu hình — running in text-only mode.")
    return NoOpTTSHandler()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def audio_to_base64(audio_bytes: bytes) -> str:
    return base64.b64encode(audio_bytes).decode("utf-8")


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 44100, channels: int = 1, sampwidth: int = 2) -> bytes:
    """Wrap raw PCM bytes in a proper WAV (RIFF) container."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)       # 2 = 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def is_wav(data: bytes) -> bool:
    return data[:4] == b'RIFF' and data[8:12] == b'WAVE'


def is_mp3(data: bytes) -> bool:
    return data[:3] == b'ID3' or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


def ensure_wav(audio_bytes: bytes, pcm_sample_rate: int = 44100) -> bytes:
    """
    Convert audio_bytes to WAV if not already.
    - WAV (RIFF): return as-is
    - Raw PCM (no header): wrap with WAV header
    - MP3: cannot convert without ffmpeg — return as-is (Rhubarb will fail gracefully)
    """
    if not audio_bytes:
        return audio_bytes
    if is_wav(audio_bytes):
        return audio_bytes
    if is_mp3(audio_bytes):
        # MP3 cannot be decoded to WAV without ffmpeg — caller handles gracefully
        return audio_bytes
    # Assume raw PCM (ElevenLabs pcm_44100 / pcm_22050 output)
    return pcm_to_wav(audio_bytes, sample_rate=pcm_sample_rate)
