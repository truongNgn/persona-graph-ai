"""
LLM Handler: abstract base + provider implementations (Ollama, DeepSeek).
Factory function `get_llm_handler(provider)` trả về handler phù hợp.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

import ollama
from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a helpful and expressive AI assistant with a warm personality.

CRITICAL INSTRUCTION — EMOTION TAGS:
For EVERY sentence you generate, you MUST prepend it with exactly one emotion tag.
Choose from this list only: [joy], [sad], [neutral], [thinking], [surprise], [anger]

Rules:
- One tag per sentence, placed at the very start.
- Match the tag to the emotional tone of that specific sentence.
- Do NOT explain the tags or mention them in conversation.
- Do NOT skip the tag for any sentence, including short responses.

Examples:
[joy] Hello there! How can I help you today?
[thinking] Let me think about that for a moment...
[neutral] The capital of France is Paris.
[surprise] Oh wow, I didn't expect that question!
[sad] I'm sorry to hear you're going through a tough time.
"""


class BaseLLMHandler(ABC):
    """Abstract base for all LLM provider handlers."""

    @abstractmethod
    async def stream_tokens(self, user_text: str) -> AsyncGenerator[str, None]:
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...

    async def warmup(self) -> None:
        """Pre-load model vào VRAM. Override ở subclass nếu cần."""


class OllamaHandler(BaseLLMHandler):
    def __init__(self, model: str | None = None) -> None:
        self._client = ollama.AsyncClient(host=settings.ollama_host)
        self.model = model or settings.ollama_model

    async def stream_tokens(self, user_text: str) -> AsyncGenerator[str, None]:
        logger.info("Ollama stream | model=%s | input=%r", self.model, user_text[:80])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        try:
            async for part in await self._client.chat(
                model=self.model,
                messages=messages,
                stream=True,
                keep_alive=settings.ollama_keep_alive,
            ):
                token: str = part["message"]["content"]
                if token:
                    yield token
        except Exception as exc:
            logger.error("Ollama stream error: %s", exc)
            raise

    async def health_check(self) -> bool:
        try:
            models = await self._client.list()
            available = [m["name"] for m in models.get("models", [])]
            if self.model not in available:
                logger.warning("Ollama model %s not found. Available: %s", self.model, available)
                return False
            return True
        except Exception as exc:
            logger.error("Ollama health check failed: %s", exc)
            return False

    async def warmup(self) -> None:
        """Gửi dummy request để Ollama load model vào VRAM trước request đầu tiên."""
        logger.info("Ollama: warming up model '%s' into VRAM...", self.model)
        try:
            # Consume toàn bộ stream của câu ngắn để đảm bảo model fully loaded
            await self._client.generate(
                model=self.model,
                prompt="Hi",
                stream=False,
                keep_alive=settings.ollama_keep_alive,
                options={"num_predict": 1},
            )
            logger.info("Ollama: warmup complete — model ready.")
        except Exception as exc:
            logger.warning("Ollama warmup failed (non-fatal): %s", exc)


class DeepSeekHandler(BaseLLMHandler):
    """DeepSeek v3 via OpenAI-compatible API (api.deepseek.com)."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
        )
        self.model = settings.deepseek_model

    async def stream_tokens(self, user_text: str) -> AsyncGenerator[str, None]:
        logger.info("DeepSeek stream | model=%s | input=%r", self.model, user_text[:80])
        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as exc:
            logger.error("DeepSeek stream error: %s", exc)
            raise

    async def health_check(self) -> bool:
        if not settings.deepseek_api_key:
            logger.warning("DeepSeek: DEEPSEEK_API_KEY not set")
            return False
        try:
            models = await self._client.models.list()
            return any(m.id == self.model for m in models.data)
        except Exception as exc:
            logger.error("DeepSeek health check failed: %s", exc)
            return False


class LocalLLMHandler(BaseLLMHandler):
    """Local OpenAI-compatible server; health checks verify served IDs."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key="not-needed", base_url=settings.local_llm_base_url,
            timeout=10.0, max_retries=0,
        )
        self.model = settings.local_llm_large_model

    async def stream_tokens(self, user_text: str) -> AsyncGenerator[str, None]:
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            max_tokens=settings.local_llm_max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def health_check(self) -> bool:
        try:
            models = await self._client.models.list()
            available = {model.id for model in models.data}
            return {settings.local_llm_large_model, settings.local_llm_small_model} <= available
        except Exception as exc:
            logger.warning("Local LLM health check failed: %s", exc)
            return False


class GeminiHandler(BaseLLMHandler):
    """Google Gemini via OpenAI-compatible API (Google AI Studio)."""

    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set; cannot use the 'gemini' LLM provider."
            )
        self._client = AsyncOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self.model = settings.gemini_model or "gemini-1.5-flash"

    async def stream_tokens(self, user_text: str) -> AsyncGenerator[str, None]:
        logger.info("Gemini stream | model=%s | input=%r", self.model, user_text[:80])
        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as exc:
            logger.error("Gemini stream error: %s", exc)
            raise

    async def health_check(self) -> bool:
        if not settings.gemini_api_key:
            logger.warning("Gemini: GEMINI_API_KEY not set")
            return False
        try:
            await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1
            )
            return True
        except Exception as exc:
            logger.error("Gemini health check failed: %s", exc)
            return False


# Legacy imports continue to work.
VLLMHandler = LocalLLMHandler

# Backwards compat alias
LLMHandler = OllamaHandler


def get_llm_handler(provider: str | None = None) -> BaseLLMHandler:
    """Factory: trả về handler theo provider string."""
    p = (provider or settings.llm_provider).lower()
    if p == "deepseek":
        return DeepSeekHandler()
    elif p in {"llama_cpp", "vllm"}:
        return LocalLLMHandler()
    elif p == "gemini":
        return GeminiHandler()
    elif p == "qwen":
        return OllamaHandler(model="qwen2.5:1.5b")
    return OllamaHandler()
