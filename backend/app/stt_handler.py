"""Speech recognition adapters, independent of the HTTP transport."""

from __future__ import annotations

import logging
import asyncio
import threading
from io import BytesIO
from abc import ABC, abstractmethod
from typing import Any

from app.config import settings
from app.models import TranscribeResult

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/ogg",
    "audio/webm",
}


class BaseSTTHandler(ABC):
    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, mime_type: str) -> TranscribeResult:
        ...

    async def warmup(self) -> None:
        """Pre-load STT resources. Override in concrete implementations."""

    @property
    def is_active(self) -> bool:
        return True


class DisabledSTTHandler(BaseSTTHandler):
    def __init__(self, reason: str = "Voice input is disabled. Set STT_ENABLED=true on the backend.") -> None:
        self.reason = reason

    @property
    def is_active(self) -> bool:
        return False

    async def transcribe(self, audio_bytes: bytes, mime_type: str) -> TranscribeResult:
        raise RuntimeError(self.reason)


class FasterWhisperSTTHandler(BaseSTTHandler):
    def __init__(self) -> None:
        self._model: Any = None
        # Serialize loading/inference: a single cached model, off the event loop.
        self._lock = threading.Lock()

    def _load_model(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                settings.stt_model,
                device=settings.stt_device,
                compute_type=settings.stt_compute_type,
            )
        return self._model

    def _warmup_sync(self) -> None:
        with self._lock:
            self._load_model()

    async def warmup(self) -> None:
        await asyncio.to_thread(self._warmup_sync)

    def _transcribe_sync(self, audio_bytes: bytes) -> TranscribeResult:
        from faster_whisper.audio import decode_audio

        try:
            samples = decode_audio(BytesIO(audio_bytes), sampling_rate=16000)
        except Exception as exc:
            raise ValueError("Could not decode the recording. Please record again.") from exc
        if len(samples) == 0:
            raise ValueError("Audio recording is empty.")
        if len(samples) / 16000 > settings.stt_max_duration_seconds:
            raise ValueError(f"Recording exceeds {settings.stt_max_duration_seconds} seconds.")

        with self._lock:
            segments, info = self._load_model().transcribe(
                samples,
                language=settings.stt_language or None,
                vad_filter=True,
                beam_size=5,
            )
            # Inference happens when the segment generator is consumed.
            text = " ".join(segment.text.strip() for segment in segments).strip()
        return TranscribeResult(
            text=text,
            language=info.language,
            confidence=info.language_probability,
        )

    async def transcribe(self, audio_bytes: bytes, mime_type: str) -> TranscribeResult:
        validate_audio_upload(audio_bytes, mime_type)
        return await asyncio.to_thread(self._transcribe_sync, audio_bytes)


def validate_audio_upload(audio_bytes: bytes, mime_type: str) -> None:
    if not audio_bytes:
        raise ValueError("Audio upload is empty.")

    max_bytes = settings.stt_max_file_mb * 1024 * 1024
    if len(audio_bytes) > max_bytes:
        raise ValueError(f"Audio upload exceeds {settings.stt_max_file_mb} MB.")

    normalized = (mime_type or "").split(";")[0].strip().lower()
    if normalized and normalized not in SUPPORTED_AUDIO_MIME_TYPES:
        raise ValueError(f"Unsupported audio MIME type: {mime_type}")


def get_stt_handler() -> BaseSTTHandler:
    if not settings.stt_enabled or settings.stt_provider == "none":
        return DisabledSTTHandler()
    if settings.stt_provider == "faster-whisper":
        return FasterWhisperSTTHandler()
    reason = f"Unsupported STT provider: {settings.stt_provider}. Use faster-whisper."
    logger.warning(reason)
    return DisabledSTTHandler(reason)
