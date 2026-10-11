import asyncio

import httpx
import pytest

from app.config import settings
from app.models import Emotion, SentenceChunk
from app.tts_handler import KokoroTTSHandler, get_tts_handler


def install_transport(monkeypatch, handle):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handle), **kwargs))


def test_kokoro_factory_and_audio_contract(monkeypatch):
    monkeypatch.setattr(settings, "tts_provider", "kokoro")
    monkeypatch.setattr(settings, "kokoro_tts_url", "http://kokoro:8080/synthesize")

    def handle(request):
        assert request.url == "http://kokoro:8080/synthesize"
        assert b'"voice": "am_michael"' in request.content or b'"voice":"am_michael"' in request.content
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"RIFF-test-audio")

    install_transport(monkeypatch, handle)
    handler = get_tts_handler()
    assert isinstance(handler, KokoroTTSHandler)
    assert handler.provider_name == "kokoro"
    chunk = SentenceChunk(text="Hello", emotion=Emotion.neutral, voice="am_michael")
    assert asyncio.run(handler.synthesize(chunk)) == b"RIFF-test-audio"


def test_service_failure_propagates_for_text_fallback(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(KokoroTTSHandler().synthesize(SentenceChunk(text="Hello")))


@pytest.mark.parametrize("status,loaded,expected", [(200, True, True), (200, False, False), (503, False, False)])
def test_health_checks_actual_model(monkeypatch, status, loaded, expected):
    def handle(request):
        assert request.url.path == "/health"
        return httpx.Response(status, json={"model_loaded": loaded})
    install_transport(monkeypatch, handle)
    assert asyncio.run(KokoroTTSHandler().health_check()) is expected
