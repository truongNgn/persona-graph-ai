"""Regression checks for local inference provider selection and served-model validation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.config import settings
from app.llm_handler import VLLMHandler, get_llm_handler
from app.model_availability import is_model_available
from app.model_registry import model_registry


def test_vllm_handler_checks_served_names(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_llm_large_model", "persona-qwen")
    monkeypatch.setattr(settings, "local_llm_small_model", "persona-qwen")
    handler = get_llm_handler("vllm")
    assert isinstance(handler, VLLMHandler)
    listing = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(id="other-model")]))
    monkeypatch.setattr(handler._client.models, "list", listing)
    assert not asyncio.run(handler.health_check())
    listing.return_value = SimpleNamespace(data=[SimpleNamespace(id="persona-qwen")])
    assert asyncio.run(handler.health_check())


def test_catalog_rejects_unserved_models(monkeypatch) -> None:
    info = model_registry.get("vllm-large")
    assert info is not None

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return httpx.Response(200, json={"data": [{"id": "another-model"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: MockClient())
    assert not asyncio.run(is_model_available(info))


def test_chat_chain_uses_local_endpoint(monkeypatch) -> None:
    from app.lc_chain import _build_llm

    monkeypatch.setattr(settings, "local_llm_base_url", "http://llama-cpp:8080/v1")
    llm = _build_llm(model_registry.get("vllm-large"))
    assert llm.openai_api_base == "http://llama-cpp:8080/v1"
    assert llm.model_name == settings.local_llm_large_model
    assert llm.max_tokens == settings.local_llm_max_tokens


def test_model_message_defaults_to_configured_provider(monkeypatch) -> None:
    from app.gateway.schemas import parse_client_message

    monkeypatch.setattr(settings, "llm_provider", "vllm")
    assert parse_client_message({"type": "set_model"}).provider == "vllm"
