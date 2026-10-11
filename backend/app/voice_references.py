"""Validate, normalize and scope reusable reference recordings."""
from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np


def owns_voice_reference(voice: str, user_id: str) -> bool:
    parts = voice.split("/")
    return (len(parts) == 2 and parts[0] == user_id and bool(parts[1])
            and parts[1] not in {".", ".."} and "\\" not in voice
            and ":" not in voice and Path(parts[1]).suffix.lower() == ".wav")


def normalize_reference(audio: bytes) -> tuple[bytes, float]:
    from faster_whisper.audio import decode_audio
    try:
        samples = decode_audio(io.BytesIO(audio), sampling_rate=16000)
    except Exception as exc:
        raise ValueError("Could not decode the voice recording.") from exc
    duration = len(samples) / 16000
    if not 5.0 < duration <= 30.0:
        raise ValueError("Record more than 5 and at most 30 seconds; 10–15 seconds is recommended.")
    if not np.isfinite(samples).all() or float(np.max(np.abs(samples))) < 0.001:
        raise ValueError("The voice recording is silent. Check your microphone.")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue(), duration
