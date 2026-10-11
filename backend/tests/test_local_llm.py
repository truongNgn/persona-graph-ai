"""Migration coverage for local serving configuration and saved selections."""
import pytest

from app.config import Settings
from app.gateway.schemas import parse_client_message
from app.lc_chain import resolve_model_info
from app.llm_handler import LocalLLMHandler, get_llm_handler
from app.model_registry import model_registry


@pytest.mark.parametrize("selection", ["llama_cpp", "local-large", "local-small", "vllm", "vllm-large", "vllm-small"])
def test_current_and_saved_model_selections(selection: str) -> None:
    assert parse_client_message({"type": "set_model", "provider": selection}).error is None
    large, small = model_registry.get_models_for_provider(selection)
    assert large.runtime == small.runtime == "openai-compatible"
    assert large.model == small.model == "persona-qwen"
    assert resolve_model_info(None, selection).id == large.id


def test_llama_cpp_health_handler() -> None:
    assert isinstance(get_llm_handler("llama_cpp"), LocalLLMHandler)


def test_legacy_environment_and_new_environment_precedence(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "http://legacy:8000/v1")
    assert Settings(_env_file=None).local_llm_base_url == "http://legacy:8000/v1"
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://llama-cpp:8080/v1")
    assert Settings(_env_file=None).local_llm_base_url == "http://llama-cpp:8080/v1"
