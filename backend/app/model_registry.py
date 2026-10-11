"""Model registry for multi-model management.

Loads model metadata from models_catalog.yaml under backend/app/
and falls back to config settings if catalog is missing.
"""

from __future__ import annotations
import asyncio

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml

from app.config import settings

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = BACKEND_ROOT / "app" / "models_catalog.yaml"

_SETTING_OVERRIDES = {
    "gemini-flash": "gemini_model",
    "deepseek-chat": "deepseek_model",
    "ollama-large": "ollama_large_model",
    "ollama-small": "ollama_small_model",
    "local-large": "local_llm_large_model",
    "local-small": "local_llm_small_model",
}


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    runtime: str  # "ollama" | "openai-compatible" | "google-genai"
    model: str  # Tên model thực tế gửi cho provider
    deployment: str  # "cloud-api" | "self-hosted" | "local"
    context_window: int | None
    tier: str  # "large" | "small"
    cost_per_1m: dict[str, float] = field(default_factory=lambda: {"input": 0.0, "output": 0.0})
    requires: list[str] = field(default_factory=list)
    fallback: str | None = None


class ModelRegistry:
    def __init__(self, catalog_path: Path = DEFAULT_CATALOG_PATH) -> None:
        self._catalog_path = catalog_path
        self._models = self._load_models()
        self._last_discovered_at = 0.0
        self._discovered_ollama_models: list[ModelInfo] = []

    def get(self, model_id: str | None) -> ModelInfo | None:
        if not model_id:
            return None
        key = model_id.lower().strip()
        key = {"vllm-large": "local-large", "vllm-small": "local-small"}.get(key, key)
        return self._models.get(key)

    def list_all(self) -> list[ModelInfo]:
        return list(self._models.values())

    def _get_required(self, model_id: str) -> ModelInfo:
        model = self.get(model_id)
        if not model:
            raise KeyError(f"Required model '{model_id}' not found in registry.")
        return model

    def get_models_for_provider(self, provider_or_model_id: str | None) -> tuple[ModelInfo, ModelInfo]:
        """Get the (large, small) models corresponding to a provider string or model ID."""
        if not provider_or_model_id:
            provider_or_model_id = settings.llm_provider

        provider_clean = provider_or_model_id.lower().strip()

        # Nếu chính là một model ID trong catalog
        model = self.get(provider_clean)
        if model:
            return model, model

        # Nếu là tên provider truyền thống
        if provider_clean == "ollama":
            return self._get_required("ollama-large"), self._get_required("ollama-small")
        elif provider_clean == "qwen":
            return self._get_required("ollama-small"), self._get_required("ollama-small")
        elif provider_clean in {"llama_cpp", "vllm"}:
            return self._get_required("local-large"), self._get_required("local-small")
        elif provider_clean == "deepseek":
            return self._get_required("deepseek-chat"), self._get_required("deepseek-chat")
        elif provider_clean == "gemini":
            return self._get_required("gemini-flash"), self._get_required("gemini-flash")

        # Fallback về mặc định
        default_provider = settings.llm_provider.lower().strip()
        if default_provider != provider_clean:
            return self.get_models_for_provider(default_provider)

        return self._get_required("ollama-large"), self._get_required("ollama-small")

    async def discover_local_models(self) -> list[ModelInfo]:
        """Discover models pull-ed locally in Ollama."""
        # Only discover if deployment_mode is local and not on cloud
        if settings.deployment_mode != "local":
            return []

        now = time.time()
        # Cache results for 30s
        if now - self._last_discovered_at < 30.0:
            return self._discovered_ollama_models

        import ollama
        try:
            client = ollama.AsyncClient(host=settings.ollama_host)
            response = await asyncio.wait_for(client.list(), timeout=1.5)
            raw_models = response.get("models", [])
        except Exception as exc:
            logger.warning("Failed to discover local Ollama models: %s", exc)
            # Return cached models if any, or empty list
            return self._discovered_ollama_models

        discovered = []
        for m in raw_models:
            capabilities = m.get("capabilities", [])
            if capabilities and "completion" not in capabilities:
                continue

            model_name = m.get("model", m.get("name", ""))
            if not model_name:
                continue
            # Older Ollama tag responses omit capabilities. Never offer the
            # configured embedding-only model as a chat option.
            if model_name.split(":")[0] == settings.embedding_model.split(":")[0]:
                continue

            param_size = ""
            details = m.get("details")
            if isinstance(details, dict):
                param_size = details.get("parameter_size", "")

            tier = _infer_tier(param_size)
            model_id = f"ollama:{model_name}"

            info = ModelInfo(
                id=model_id,
                display_name=f"{model_name} (Local)",
                runtime="ollama",
                model=model_name,
                deployment="local",
                context_window=8192,  # Cautionary level
                tier=tier,
            )
            discovered.append(info)
            # Register in registry so build_chain can look it up
            self._models[model_id] = info

        self._discovered_ollama_models = discovered
        self._last_discovered_at = now
        return discovered


    def _load_models(self) -> dict[str, ModelInfo]:
        loaded = self._load_from_yaml()
        if loaded:
            return loaded

        # Fallback cứng khi không load được catalog
        fallback_data = [
            {
                "id": "gemini-flash",
                "display_name": "Google Gemini 2.5 Flash",
                "runtime": "google-genai",
                "model": settings.gemini_model or "gemini-2.5-flash",
                "deployment": "cloud-api",
                "context_window": 1000000,
                "tier": "large",
                "cost_per_1m": {"input": 0.30, "output": 2.50},
                "requires": ["gemini_api_key"],
                "fallback": "deepseek-chat"
            },
            {
                "id": "deepseek-chat",
                "display_name": "DeepSeek Chat",
                "runtime": "openai-compatible",
                "model": settings.deepseek_model or "deepseek-chat",
                "deployment": "cloud-api",
                "context_window": 64000,
                "tier": "large",
                "cost_per_1m": {"input": 0.14, "output": 0.28},
                "requires": ["deepseek_api_key"],
                "fallback": "gemini-flash"
            },
            {
                "id": "local-large",
                "display_name": "Local LLM Large",
                "runtime": "openai-compatible",
                "model": settings.local_llm_large_model or "persona-qwen",
                "deployment": "self-hosted",
                "context_window": 4096,
                "tier": "large",
                "fallback": None
            },
            {
                "id": "local-small",
                "display_name": "Local LLM Small",
                "runtime": "openai-compatible",
                "model": settings.local_llm_small_model or "persona-qwen",
                "deployment": "self-hosted",
                "context_window": 4096,
                "tier": "small"
            },
            {
                "id": "ollama-large",
                "display_name": "Ollama Large (Llama 3.1)",
                "runtime": "ollama",
                "model": settings.ollama_large_model or "llama3.1:latest",
                "deployment": "local",
                "context_window": 8192,
                "tier": "large",
                "fallback": "ollama-small"
            },
            {
                "id": "ollama-small",
                "display_name": "Ollama Small (Qwen 2.5)",
                "runtime": "ollama",
                "model": settings.ollama_small_model or "qwen2.5:1.5b",
                "deployment": "local",
                "context_window": 32768,
                "tier": "small"
            }
        ]

        logger.warning(
            "Model catalog file not found or empty at %s; using default fallbacks.",
            self._catalog_path,
        )
        return self._parse_items(fallback_data)

    def _load_from_yaml(self) -> dict[str, ModelInfo] | None:
        if not self._catalog_path.exists():
            return None

        try:
            with open(self._catalog_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except Exception as exc:
            logger.warning("Failed to read model catalog %s: %s", self._catalog_path, exc)
            return None

        if not isinstance(raw, list):
            logger.warning("Model catalog %s must contain a list of models.", self._catalog_path)
            return None

        return self._parse_items(raw)

    def _parse_items(self, items: list[dict[str, Any]]) -> dict[str, ModelInfo]:
        models: dict[str, ModelInfo] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "")).strip().lower()
            if not model_id:
                continue

            # Override model name from settings if defined
            model_name = str(item.get("model", ""))
            setting_name = _SETTING_OVERRIDES.get(model_id)
            if setting_name:
                override = getattr(settings, setting_name, None)
                if override:
                    model_name = override

            cost = item.get("cost_per_1m") or {"input": 0.0, "output": 0.0}
            requires = item.get("requires") or []

            models[model_id] = ModelInfo(
                id=model_id,
                display_name=str(item.get("display_name") or model_id),
                runtime=str(item.get("runtime", "ollama")),
                model=model_name,
                deployment=str(item.get("deployment", "local")),
                context_window=item.get("context_window"),
                tier=str(item.get("tier", "large")),
                cost_per_1m=cost,
                requires=requires,
                fallback=item.get("fallback")
            )
        return models


def _infer_tier(param_size_str: str) -> str:
    """Infer model tier (small or large) from Ollama's parameter size string.

    Examples: '1.5B', '3.2B', '8B', '137M'.
    We consider anything < 3.0B as 'small', and >= 3.0B as 'large'.
    """
    if not param_size_str:
        return "large"

    match = re.search(r"([0-9.]+)([a-zA-Z]*)", param_size_str)
    if not match:
        return "large"

    num_str, unit = match.groups()
    try:
        val = float(num_str)
    except ValueError:
        return "large"

    unit = unit.upper()
    if unit == "M":
        return "small"
    elif unit == "B":
        if val < 3.0:
            return "small"
        return "large"

    return "large"


model_registry = ModelRegistry()
