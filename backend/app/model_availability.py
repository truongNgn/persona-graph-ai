"""Verify actual served model names, not just inference server reachability."""
import httpx

from app.config import settings
from app.model_registry import ModelInfo


async def is_model_available(info: ModelInfo) -> bool:
    if info.runtime not in {"ollama", "openai-compatible"} or info.id == "deepseek-chat":
        return True
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            if info.runtime == "ollama":
                response = await client.get(f"{settings.ollama_host.rstrip('/')}/api/tags")
                response.raise_for_status()
                names = {item.get("name", item.get("model")) for item in response.json().get("models", [])}
                return info.model in names or f"{info.model}:latest" in names
            response = await client.get(f"{settings.local_llm_base_url.rstrip('/')}/models")
            response.raise_for_status()
            return info.model in {item.get("id") for item in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False
