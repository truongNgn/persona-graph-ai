import asyncio
import io
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app import stt_handler


def test_factory_respects_enabled_and_provider(monkeypatch):
    monkeypatch.setattr(stt_handler.settings, "stt_enabled", False)
    assert not stt_handler.get_stt_handler().is_active
    monkeypatch.setattr(stt_handler.settings, "stt_enabled", True)
    monkeypatch.setattr(stt_handler.settings, "stt_provider", "faster-whisper")
    assert isinstance(stt_handler.get_stt_handler(), stt_handler.FasterWhisperSTTHandler)
    monkeypatch.setattr(stt_handler.settings, "stt_provider", "unknown")
    assert "Unsupported" in stt_handler.get_stt_handler().reason


def test_transcription_consumes_segments_and_passes_language(monkeypatch):
    import faster_whisper.audio

    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **kw: np.zeros(16000))
    monkeypatch.setattr(stt_handler.settings, "stt_language", "vi")
    model = Mock()
    model.transcribe.return_value = (
        iter([SimpleNamespace(text=" Xin chào "), SimpleNamespace(text=" bạn. ")]),
        SimpleNamespace(language="vi", language_probability=0.99),
    )
    handler = stt_handler.FasterWhisperSTTHandler()
    handler._model = model
    result = asyncio.run(handler.transcribe(b"recording", "audio/webm;codecs=opus"))
    assert result.text == "Xin chào bạn."
    assert result.language == "vi"
    assert model.transcribe.call_args.kwargs["vad_filter"] is True
    assert model.transcribe.call_args.kwargs["language"] == "vi"


def test_duration_limit_before_inference(monkeypatch):
    import faster_whisper.audio

    monkeypatch.setattr(stt_handler.settings, "stt_max_duration_seconds", 1)
    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **kw: np.zeros(32000))
    handler = stt_handler.FasterWhisperSTTHandler()
    with pytest.raises(ValueError, match="exceeds 1 seconds"):
        asyncio.run(handler.transcribe(b"recording", "audio/wav"))
    assert handler._model is None


def test_upload_validation(monkeypatch):
    with pytest.raises(ValueError, match="empty"):
        stt_handler.validate_audio_upload(b"", "audio/webm")
    with pytest.raises(ValueError, match="Unsupported"):
        stt_handler.validate_audio_upload(b"x", "text/plain")
    monkeypatch.setattr(stt_handler.settings, "stt_max_file_mb", 1)
    with pytest.raises(ValueError, match="exceeds"):
        stt_handler.validate_audio_upload(b"x" * (1024 * 1024 + 1), "audio/wav")


def test_model_cached_across_warmups(monkeypatch):
    import faster_whisper

    factory = Mock(return_value=Mock())
    monkeypatch.setattr(faster_whisper, "WhisperModel", factory)
    handler = stt_handler.FasterWhisperSTTHandler()
    asyncio.run(handler.warmup())
    asyncio.run(handler.warmup())
    assert factory.call_count == 1


@pytest.mark.parametrize("mime_type", ["audio/wav", "audio/webm;codecs=opus"])
def test_real_audio_decoding(mime_type):
    import av

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    audio_bytes = wav_buffer.getvalue()
    if mime_type.startswith("audio/webm"):
        webm_buffer = io.BytesIO()
        with av.open(io.BytesIO(audio_bytes)) as source, av.open(webm_buffer, mode="w", format="webm") as target:
            stream = target.add_stream("libopus", rate=48000)
            resampler = av.AudioResampler(format="flt", layout="mono", rate=48000)
            for frame in source.decode(audio=0):
                for converted in resampler.resample(frame):
                    for packet in stream.encode(converted):
                        target.mux(packet)
            for converted in resampler.resample(None):
                for packet in stream.encode(converted):
                    target.mux(packet)
            for packet in stream.encode(None):
                target.mux(packet)
        audio_bytes = webm_buffer.getvalue()
    handler = stt_handler.FasterWhisperSTTHandler()
    model = Mock()
    model.transcribe.return_value = (iter([]), SimpleNamespace(language="en", language_probability=1.0))
    handler._model = model
    result = asyncio.run(handler.transcribe(audio_bytes, mime_type))
    assert result.text == ""
    samples = model.transcribe.call_args.args[0]
    assert samples.dtype == np.float32
    assert 15000 <= len(samples) <= 17000
