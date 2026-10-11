"""LangChain chain: provider LLM + ChatPromptTemplate + session history."""

from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables.history import RunnableWithMessageHistory
from app.config import settings
from app.session_history import get_session_history
from app.model_registry import model_registry, ModelInfo

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# 2. Prompt template — system_prompt được inject động từ lc_graph.py (build_system_prompt)
#    bao gồm: persona + emotion_rules + memory_context (nếu có)
prompt = ChatPromptTemplate.from_messages([
    ("system", "{system_prompt}"),
    MessagesPlaceholder("history"),
    ("human", "{user_input}"),
])


def normalize_provider(provider: str | None = None) -> str:
    """Provider dùng cho lượt chat hiện tại; None → giá trị trong .env."""
    return (provider or settings.llm_provider).lower().strip()


def resolve_model_info(model: str | None, provider: str | None) -> ModelInfo:
    """Resolve model and provider parameters to a ModelInfo instance.

    Handles model IDs, legacy provider names, and falls back to system defaults.
    Also supports custom raw model names passed dynamically.
    """
    registry = model_registry
    prov_clean = normalize_provider(provider)

    if model:
        model_clean = model.strip()
        # Direct lookup by model ID
        info = registry.get(model_clean)
        if info:
            return info

        # Lookup by raw model name
        for m in registry.list_all():
            if m.model.lower().strip() == model_clean.lower():
                return m

        # Fallback for dynamic/custom model strings not in catalog
        runtime = "ollama"
        deployment = "local"
        if prov_clean == "gemini" or "gemini" in model_clean.lower():
            runtime = "google-genai"
            deployment = "cloud-api"
        elif prov_clean == "deepseek" or prov_clean in {"llama_cpp", "vllm"} or "deepseek" in model_clean.lower():
            runtime = "openai-compatible"
            deployment = "cloud-api" if prov_clean == "deepseek" else "self-hosted"

        return ModelInfo(
            id=f"dynamic:{model_clean}",
            display_name=model_clean,
            runtime=runtime,
            model=model_clean,
            deployment=deployment,
            context_window=8192,
            tier="large",
        )

    # Resolve default model for provider/runtime
    registered = registry.get(prov_clean)
    if registered:
        return registered

    if prov_clean == "gemini":
        return registry._get_required("gemini-flash")
    elif prov_clean == "deepseek":
        return registry._get_required("deepseek-chat")
    elif prov_clean in {"llama_cpp", "vllm"}:
        return registry._get_required("local-large")
    elif prov_clean == "qwen":
        return registry._get_required("ollama-small")
    elif prov_clean == "ollama":
        return registry._get_required("ollama-large")

    # Fallback to system-configured provider
    default_prov = settings.llm_provider.lower().strip()
    if default_prov != prov_clean:
        return resolve_model_info(None, default_prov)

    return registry._get_required("ollama-large")


def _build_llm(info: ModelInfo) -> BaseChatModel:
    """Chat model của LangChain cho runtime — mọi nhánh đều được LangSmith trace."""
    if info.runtime == "openai-compatible":
        if info.id == "deepseek-chat" or "deepseek" in info.model.lower():
            return ChatOpenAI(
                model=info.model,
                base_url=DEEPSEEK_BASE_URL,
                api_key=settings.deepseek_api_key,
                streaming=True,
            )
        else:
            # Local OpenAI-compatible inference
            return ChatOpenAI(
                model=info.model,
                base_url=settings.local_llm_base_url,
                api_key="not-needed",
                streaming=True,
                max_tokens=settings.local_llm_max_tokens,
            )
    elif info.runtime == "google-genai":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=info.model,
            google_api_key=settings.gemini_api_key,
            streaming=True,
        )
    # Default to ollama
    return ChatOllama(
        model=info.model,
        base_url=settings.ollama_host,
        keep_alive=settings.ollama_keep_alive,
    )


def _build_llm_with_fallback(info: ModelInfo, visited: set[str] | None = None) -> BaseChatModel:
    """Build the chat model and wrap it in fallback runnables if configured and compatible."""
    if visited is None:
        visited = set()
    
    visited.add(info.id)
    primary = _build_llm(info)
    
    if info.fallback:
        fallback_info = model_registry.get(info.fallback)
        if fallback_info and fallback_info.id not in visited:
            # Check environment compatibility for the fallback model
            is_compatible = True
            if settings.deployment_mode == "cloud" and fallback_info.deployment == "local":
                is_compatible = False
            for req in fallback_info.requires:
                if not getattr(settings, req, None):
                    is_compatible = False
                    break
                    
            if is_compatible:
                fallback_llm = _build_llm_with_fallback(fallback_info, visited)
                return primary.with_fallbacks([fallback_llm])
                
    return primary


def build_chain(
    model: str | None = None,
    provider: str | None = None,
) -> RunnableWithMessageHistory:
    """
    Build a RunnableWithMessageHistory chain for the selected provider/model.

    Pass provider=None to use LLM_PROVIDER from .env, model=None to use that
    provider's default model. Chains are cached by resolved model ID to
    avoid recreating clients every turn.
    """
    info = resolve_model_info(model, provider)
    cache_key = info.id

    if cache_key not in _chain_cache:
        base_chain = prompt | _build_llm_with_fallback(info) | StrOutputParser()
        _chain_cache[cache_key] = RunnableWithMessageHistory(
            base_chain,
            get_session_history,
            input_messages_key="user_input",
            history_messages_key="history",
        )
    return _chain_cache[cache_key]


# Cache: provider:model_name → RunnableWithMessageHistory
_chain_cache: dict[str, RunnableWithMessageHistory] = {}


def __getattr__(name: str):
    """
    Backward-compatible singleton `chain`, dựng lazy ở lần truy cập đầu tiên.

    Dựng eager lúc import khiến một provider cấu hình sai làm hỏng cả
    `import app.lc_graph`, và khi đó `graph.ainvoke()` không bao giờ chạy nên
    LangSmith không nhận được run nào. Lazy thì lỗi nổ trong generate_node và
    vẫn được trace lại dưới dạng run lỗi.
    """
    if name == "chain":
        return build_chain()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
