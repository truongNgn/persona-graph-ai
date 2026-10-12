"""Optional Langfuse callbacks, isolated from the chat transport and providers."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler

from app.config import settings, validated_langfuse_base_url

if TYPE_CHECKING:
    from langfuse import Langfuse

logger = logging.getLogger(__name__)
_client: Langfuse | None = None
_initialization_attempted = False


def initialize_langfuse() -> Langfuse | None:
    """Initialize once without network checks or making tracing required for chat."""
    global _client, _initialization_attempted
    if not settings.langfuse_enabled:
        return None
    if _initialization_attempted:
        return _client
    _initialization_attempted = True
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.warning("Langfuse enabled but project keys are missing; tracing disabled.")
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=validated_langfuse_base_url(),
            environment=settings.langfuse_environment,
            timeout=5,
        )
        logger.info("Langfuse tracing configured (environment=%s).", settings.langfuse_environment)
    except Exception:
        logger.warning("Langfuse initialization failed; chat continues without tracing.")
    return _client


def langfuse_callbacks() -> list[BaseCallbackHandler]:
    """A fresh handler per turn prevents concurrent run state from being shared."""
    if initialize_langfuse() is None:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        handler = CallbackHandler(public_key=settings.langfuse_public_key)
        handler.raise_error = False
        return [handler]
    except Exception:
        logger.warning("Langfuse callback unavailable; chat continues without tracing.")
        return []


def langfuse_metadata(user_id: str, character_id: str, session_id: str) -> dict[str, Any]:
    """Scope dashboard sessions to the same user/character boundary as history."""
    from app.session_history import build_history_key

    return {
        "langfuse_user_id": user_id,
        "langfuse_session_id": build_history_key(user_id, character_id, session_id),
        "langfuse_tags": ["persona-graph-ai", f"character:{character_id}"],
    }


async def shutdown_langfuse() -> None:
    """Drain background exports at shutdown, off the FastAPI event loop."""
    global _client, _initialization_attempted
    client = _client
    _client = None
    _initialization_attempted = False
    if client is not None:
        try:
            await asyncio.to_thread(client.shutdown)
        except Exception:
            logger.warning("Langfuse shutdown failed; pending traces may be lost.")
