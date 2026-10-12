import asyncio
import io
import json
import wave

import httpx
import numpy as np
import pytest
from fastapi import HTTPException, UploadFile

from app.auth import AuthContext
from app.config import settings
from app.gateway.schemas import parse_client_message
from app.models import SentenceChunk
from app.tts_handler import ChatterboxNanoTTSHandler, get_tts_handler
from app.voice_references import normalize_reference, owns_voice_reference


def recording(seconds=10, silent=False):
    samples = np.zeros(int(seconds * 16000), dtype=np.int16)
    if not silent:
        samples = (np.sin(np.arange(len(samples)) * 0.1) * 4000).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


@pytest.mark.parametrize("seconds", [2, 5, 31])
def test_reference_duration_rejected(seconds):
    with pytest.raises(ValueError, match="seconds"):
        normalize_reference(recording(seconds))


def test_reference_normalization_and_silence():
    data, duration = normalize_reference(recording())
    assert duration == 10
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnchannels() == 1 and wav.getframerate() == 16000
    with pytest.raises(ValueError, match="silent"):
        normalize_reference(recording(silent=True))


@pytest.mark.parametrize("voice", ["other/clip.wav", "u/../secret.wav", "u/..\\secret.wav", "/u/clip.wav", "u/C:clip.wav"])
def test_reference_ownership_and_gateway(voice, monkeypatch):
    assert not owns_voice_reference(voice, "u")
    monkeypatch.setattr(settings, "tts_provider", "chatterbox-nano")
    parsed = parse_client_message({"type": "user_message", "text": "hello", "voice": voice}, authenticated_user_id="u")
    assert parsed.error


def test_cloning_and_preset_fallback_routing(monkeypatch):
    monkeypatch.setattr(settings, "tts_provider", "chatterbox-nano")
    monkeypatch.setattr(settings, "chatterbox_tts_url", "http://nano/synthesize")
    monkeypatch.setattr(settings, "kokoro_tts_url", "http://kokoro/synthesize")
    calls = []
    client_type = httpx.AsyncClient

    def handle(request):
        calls.append((request.url.host, json.loads(request.content)["voice"]))
        if request.url.host == "nano":
            return httpx.Response(503)
        return httpx.Response(200, content=b"fallback-audio")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
    handler = get_tts_handler()
    assert isinstance(handler, ChatterboxNanoTTSHandler)
    assert asyncio.run(handler.synthesize(SentenceChunk(text="hi", voice="u/clip.wav"))) == b"fallback-audio"
    assert calls == [("nano", "u/clip.wav"), ("kokoro", "af_bella")]
    calls.clear()
    asyncio.run(handler.synthesize(SentenceChunk(text="hi", voice="am_michael")))
    assert calls == [("kokoro", "am_michael")]


def test_upload_reusable_reference_and_private_preview(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "tts_provider", "chatterbox-nano")
    monkeypatch.setattr(main, "BACKEND_ROOT", tmp_path)
    auth = AuthContext(user_id="u", mode="jwt")
    upload = UploadFile(filename="reference.wav", file=io.BytesIO(recording()))
    saved = asyncio.run(main.upload_voice(file=upload, auth=auth))
    assert owns_voice_reference(saved["filename"], "u")
    assert (tmp_path / "voices" / saved["filename"]).is_file()
    with pytest.raises(HTTPException) as failure:
        asyncio.run(main.preview_voice(main.VoicePreviewRequest(voice=saved["filename"]), AuthContext(user_id="other", mode="jwt")))
    assert failure.value.status_code == 403


def test_preview_does_not_silently_substitute_speaker(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "tts_provider", "chatterbox-nano")
    monkeypatch.setattr(main, "BACKEND_ROOT", tmp_path)
    (tmp_path / "voices" / "u").mkdir(parents=True)
    (tmp_path / "voices" / "u" / "clip.wav").write_bytes(recording())
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kw))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(main.preview_voice(main.VoicePreviewRequest(voice="u/clip.wav"), AuthContext(user_id="u", mode="jwt")))
    assert failure.value.status_code == 503


def test_voice_discovery_reports_failed_model(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "tts_provider", "chatterbox-nano")
    monkeypatch.setattr(main, "BACKEND_ROOT", tmp_path)
    client_type = httpx.AsyncClient
    def handle(request):
        if request.url.path == "/voices":
            return httpx.Response(200, json={"voices": ["af_bella"]})
        return httpx.Response(503, json={"model_loaded": False, "status": "failed"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
    result = asyncio.run(main.get_voices(AuthContext(user_id="u", mode="jwt")))
    assert result["supports_voice_cloning"] is True
    assert result["clone_ready"] is False and result["clone_status"] == "failed"
