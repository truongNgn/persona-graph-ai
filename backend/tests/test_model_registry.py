import pytest
from app.model_registry import ModelRegistry, ModelInfo
from app.lc_chain import resolve_model_info, build_chain
from app.config import settings


def test_model_registry_loads_models() -> None:
    # Use default constructor which loads models_catalog.yaml
    registry = ModelRegistry()
    
    # Assert standard models exist
    ollama_large = registry.get("ollama-large")
    assert ollama_large is not None
    assert ollama_large.runtime == "ollama"
    assert ollama_large.tier == "large"
    
    gemini = registry.get("gemini-flash")
    assert gemini is not None
    assert gemini.runtime == "google-genai"
    assert gemini.fallback == "deepseek-chat"


def test_model_registry_get_models_for_provider() -> None:
    registry = ModelRegistry()
    
    # Provider: ollama
    large, small = registry.get_models_for_provider("ollama")
    assert large.id == "ollama-large"
    assert small.id == "ollama-small"

    # Provider: qwen
    large, small = registry.get_models_for_provider("qwen")
    assert large.id == "ollama-small"
    assert small.id == "ollama-small"

    # Provider: gemini
    large, small = registry.get_models_for_provider("gemini")
    assert large.id == "gemini-flash"
    assert small.id == "gemini-flash"

    # Direct model ID
    large, small = registry.get_models_for_provider("deepseek-chat")
    assert large.id == "deepseek-chat"
    assert small.id == "deepseek-chat"


def test_model_settings_override(monkeypatch) -> None:
    # Override settings before instantiating ModelRegistry
    monkeypatch.setattr(settings, "gemini_model", "gemini-1.5-pro-custom")
    monkeypatch.setattr(settings, "ollama_large_model", "custom-llama3:8b")

    registry = ModelRegistry()
    
    gemini = registry.get("gemini-flash")
    assert gemini.model == "gemini-1.5-pro-custom"
    
    ollama_large = registry.get("ollama-large")
    assert ollama_large.model == "custom-llama3:8b"


def test_resolve_model_info() -> None:
    # 1. Custom model string containing 'gemini'
    info = resolve_model_info("gemini-custom-model", "gemini")
    assert info.runtime == "google-genai"
    assert info.model == "gemini-custom-model"
    assert info.id == "dynamic:gemini-custom-model"

    # 2. Lookup by raw model name matching catalog
    info = resolve_model_info("qwen2.5:1.5b", None)
    assert info.id == "ollama-small"

    # 3. Model is None, provider is gemini
    info = resolve_model_info(None, "gemini")
    assert info.id == "gemini-flash"


def test_build_chain_caching() -> None:
    # Test that build_chain returns RunnableWithMessageHistory
    chain1 = build_chain(None, "ollama")
    chain2 = build_chain("ollama-large", None)
    
    # Should resolve to the same model (ollama-large) and return cached chain
    assert chain1 is chain2


def test_infer_tier() -> None:
    from app.model_registry import _infer_tier
    assert _infer_tier("1.5B") == "small"
    assert _infer_tier("2.5B") == "small"
    assert _infer_tier("3.0B") == "large"
    assert _infer_tier("8.0B") == "large"
    assert _infer_tier("137M") == "small"
    assert _infer_tier("") == "large"
    assert _infer_tier("invalid") == "large"


class MockOllamaClient:
    def __init__(self, host=None):
        pass
    async def list(self):
        return {
            "models": [
                {
                    "model": "mistral:latest",
                    "details": {"parameter_size": "7.2B"},
                    "capabilities": ["completion", "tools"]
                },
                {
                    "model": "nomic-embed-text:latest",
                    "details": {"parameter_size": "137M"},
                    # Real /api/tags responses may omit capabilities.
                },
                {
                    "model": "tinyllama:latest",
                    "details": {"parameter_size": "1.1B"},
                    "capabilities": ["completion"]
                }
            ]
        }


@pytest.mark.anyio
async def test_discover_local_models(monkeypatch) -> None:
    import ollama
    monkeypatch.setattr(ollama, "AsyncClient", MockOllamaClient)
    
    registry = ModelRegistry()
    monkeypatch.setattr(settings, "deployment_mode", "local")
    
    discovered = await registry.discover_local_models()
    
    assert len(discovered) == 2
    
    mistral = registry.get("ollama:mistral:latest")
    assert mistral is not None
    assert mistral.tier == "large"
    assert mistral.deployment == "local"
    
    tinyllama = registry.get("ollama:tinyllama:latest")
    assert tinyllama is not None
    assert tinyllama.tier == "small"
    assert tinyllama.deployment == "local"


def test_get_llm_models_endpoint(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from app.main import app
    
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "gemini_api_key", "mock-key")
    
    # Mock reachability checks so we don't hit external APIs
    async def mock_is_reachable(runtime: str, model_id: str) -> bool:
        return True
    
    # We can inject this mock or just let the endpoint execute.
    # Since gemini-flash has runtime google-genai, it returns True immediately.
    # So we don't even need to mock httpx reachability.
    client = TestClient(app)
    response = client.get("/api/llm/models")
    assert response.status_code == 200
    
    data = response.json()
    assert "default" in data
    assert "groups" in data
    
    groups = data["groups"]
    for group in groups:
        assert group["label"] != "Local (Ollama)"


def test_parse_client_message_set_model() -> None:
    from app.gateway.schemas import parse_client_message
    
    parsed = parse_client_message({"type": "set_model", "provider": "gemini"})
    assert parsed.error is None
    assert parsed.provider == "gemini"

    parsed = parse_client_message({"type": "set_model", "provider": "gemini-flash"})
    assert parsed.error is None
    assert parsed.provider == "gemini-flash"

    parsed = parse_client_message({"type": "set_model", "provider": "invalid-model"})
    assert parsed.error is not None
    assert "Unknown provider or model ID" in parsed.error


def test_websocket_set_model_preflight(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import issue_dev_token
    
    token = issue_dev_token("test-user")
    
    # Use TestClient context manager to trigger lifespan events
    with TestClient(app) as client:
        # 1. Test invalid model
        with client.websocket_connect(f"/ws/chat?token={token}") as websocket:
            conn_msg = websocket.receive_json()
            assert conn_msg["type"] == "connected"
            
            websocket.send_json({"type": "set_model", "provider": "invalid-model"})
            resp = websocket.receive_json()
            assert resp["type"] == "error"
            assert "Unknown provider or model ID" in resp["message"]

        # 2. Test local model in cloud mode
        monkeypatch.setattr(settings, "deployment_mode", "cloud")
        with client.websocket_connect(f"/ws/chat?token={token}") as websocket:
            websocket.receive_json()
            
            websocket.send_json({"type": "set_model", "provider": "ollama"})
            resp = websocket.receive_json()
            assert resp["type"] == "error"
            assert "only available locally, not in cloud mode" in resp["message"]

        # 3. Test model with missing API keys
        monkeypatch.setattr(settings, "deployment_mode", "cloud")
        monkeypatch.setattr(settings, "deepseek_api_key", "")
        with client.websocket_connect(f"/ws/chat?token={token}") as websocket:
            websocket.receive_json()
            
            websocket.send_json({"type": "set_model", "provider": "deepseek"})
            resp = websocket.receive_json()
            assert resp["type"] == "error"
            assert "Missing API key" in resp["message"]


def test_llm_fallback_chain() -> None:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatResult, ChatGeneration
    
    class FailingChatModel(BaseChatModel):
        called: bool = False
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.called = True
            raise ValueError("Primary API failed")
            
        @property
        def _llm_type(self) -> str:
            return "failing-mock"

    class SucceedingChatModel(BaseChatModel):
        called: bool = False
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.called = True
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Fallback response"))])
            
        @property
        def _llm_type(self) -> str:
            return "succeeding-mock"

    primary_model = FailingChatModel()
    fallback_model = SucceedingChatModel()
    
    chain = primary_model.with_fallbacks([fallback_model])
    res = chain.invoke("Hello")
    
    assert primary_model.called is True
    assert fallback_model.called is True
    assert res.content == "Fallback response"


def test_build_llm_with_fallback_environment_compatibility(monkeypatch) -> None:
    from app import lc_chain
    from app.lc_chain import _build_llm_with_fallback
    from app.model_registry import model_registry
    from langchain_core.language_models import BaseChatModel
    
    class SimpleMockModel(BaseChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            pass
        @property
        def _llm_type(self) -> str:
            return "mock"
            
    monkeypatch.setattr(lc_chain, "_build_llm", lambda info: SimpleMockModel())
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    
    gemini_info = model_registry.get("gemini-flash")
    assert gemini_info.fallback == "deepseek-chat"
    
    llm = _build_llm_with_fallback(gemini_info)
    
    from langchain_core.runnables import RunnableWithFallbacks
    assert not isinstance(llm, RunnableWithFallbacks)


def test_build_llm_with_fallback_recursion_safety(monkeypatch) -> None:
    from app import lc_chain
    from app.lc_chain import _build_llm_with_fallback
    from app.model_registry import model_registry
    from langchain_core.language_models import BaseChatModel
    
    class SimpleMockModel(BaseChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            pass
        @property
        def _llm_type(self) -> str:
            return "mock"
            
    monkeypatch.setattr(lc_chain, "_build_llm", lambda info: SimpleMockModel())
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "gemini_api_key", "mock-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "mock-key")
    
    gemini_info = model_registry.get("gemini-flash")
    
    llm = _build_llm_with_fallback(gemini_info)
    
    from langchain_core.runnables import RunnableWithFallbacks
    assert isinstance(llm, RunnableWithFallbacks)
    assert len(llm.fallbacks) == 1
    
    fallback_model = llm.fallbacks[0]
    assert not isinstance(fallback_model, RunnableWithFallbacks)


def test_token_cost_tracking_feedback_event(monkeypatch) -> None:
    from app.feedback import default_feedback_store, FeedbackEvent
    from app.lc_graph import generate_node
    import asyncio
    
    events = []
    async def mock_record_event(event: FeedbackEvent) -> None:
        events.append(event)
    monkeypatch.setattr(default_feedback_store, "record_event", mock_record_event)
    
    from app import lc_graph
    from langchain_core.runnables import RunnableLambda
    
    def mock_chain_call(inputs):
        return "This is a mock response from the LLM."
    
    mock_runnable = RunnableLambda(mock_chain_call)
    
    class MockRunnableWithMessageHistory:
        def __init__(self, base):
            self.base = base
        async def astream(self, inputs, config=None):
            text = self.base.invoke(inputs)
            for char in text.split(" "):
                yield char + " "
                
    monkeypatch.setattr(lc_graph, "build_chain", lambda model, provider: MockRunnableWithMessageHistory(mock_runnable))
    
    q = asyncio.Queue()
    state = {
        "user_id": "test-user-cost",
        "session_id": "session-cost",
        "character_id": "char-cost",
        "user_text": "Calculate my cost please",
        "system_prompt": "You are a cost tracking assistant",
        "token_queue": q,
        "turn_id": "turn-cost",
        "selected_model": "gemini-flash",
        "provider": "gemini",
    }
    
    # Run the generate_node coroutine in the running loop or using anyio/asyncio
    res = asyncio.run(generate_node(state))
    
    # Fetch from queue
    tokens = []
    while True:
        token = asyncio.run(q.get())
        if token is None:
            break
        tokens.append(token)
        
    assert len(tokens) > 0
    assert len(events) == 1
    
    event = events[0]
    assert event.event_type == "token_usage"
    assert event.user_id == "test-user-cost"
    assert event.turn_id == "turn-cost"
    
    payload = event.payload
    assert payload["model_id"] == "gemini-flash"
    assert payload["input_tokens"] == 14
    assert payload["output_tokens"] == 9
    assert abs(payload["input_cost"] - 0.0000042) < 1e-9
    assert abs(payload["output_cost"] - 0.0000225) < 1e-9
    assert abs(payload["total_cost"] - 0.0000267) < 1e-9
    assert payload["interrupted"] is False


@pytest.mark.anyio
async def test_startup_guard_secrets(monkeypatch) -> None:
    from app.main import lifespan
    from fastapi import FastAPI
    app = FastAPI()
    
    # 1. Cloud mode - missing keys -> raises ValueError
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "auth_token_secret", "secure-key")
    
    with pytest.raises(ValueError) as exc:
        async with lifespan(app):
            pass
    assert "Startup Guard: Missing required cloud secrets/keys" in str(exc.value)

    # 1b. Cloud mode - default auth_token_secret -> raises ValueError
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-key")
    monkeypatch.setattr(settings, "auth_token_secret", "change-me-dev-secret")
    
    with pytest.raises(ValueError) as exc:
        async with lifespan(app):
            pass
    assert "AUTH_TOKEN_SECRET" in str(exc.value)
    
    # 2. Local mode - missing keys -> does NOT raise ValueError
    monkeypatch.setattr(settings, "deployment_mode", "local")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "auth_token_secret", "change-me-dev-secret")
    
    import app.main as app_main
    monkeypatch.setattr(app_main, "get_tts_handler", lambda: type("MockTTS", (), {"is_active": False})())
    monkeypatch.setattr(app_main, "get_stt_handler", lambda: type("MockSTT", (), {"is_active": False})())
    async def mock_init_db():
        return True
    monkeypatch.setattr(app_main, "init_db", mock_init_db)
    monkeypatch.setattr(app_main, "start_background_warmup", lambda tts, stt: None)
    
    async with lifespan(app):
        pass




