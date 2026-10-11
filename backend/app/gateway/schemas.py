"""Gateway payload normalization for the existing WebSocket contract."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel

from app.character_registry import character_registry
from app.config import SUPPORTED_LLM_PROVIDERS, settings


class ChatRequest(BaseModel):
    text: str
    user_id: str
    session_id: str
    character_id: str
    tts_enabled: bool = True
    router_enabled: bool = True
    voice: str | None = None
    turn_id: str


class MessageParseResult(BaseModel):
    type: str
    request: ChatRequest | None = None
    provider: str | None = None
    error: str | None = None


def parse_client_message(data: dict, authenticated_user_id: str | None = None) -> MessageParseResult:
    msg_type = data.get("type", "")

    if msg_type == "interrupt":
        return MessageParseResult(type="interrupt")

    if msg_type == "set_model":
        provider = str(data.get("provider", settings.llm_provider)).lower().strip()
        from app.model_registry import model_registry
        valid_legacy = set(SUPPORTED_LLM_PROVIDERS)
        if provider not in valid_legacy and not model_registry.get(provider):
            return MessageParseResult(type="set_model", error=f"Unknown provider or model ID: {provider}")
        return MessageParseResult(type="set_model", provider=provider)

    if msg_type == "user_message":
        if not authenticated_user_id:
            return MessageParseResult(type="user_message", error="Authentication required.")

        user_text = str(data.get("text", "")).strip()
        if not user_text:
            return MessageParseResult(type="user_message", error="Empty message")

        character_id = str(data.get("character_id", settings.default_character_id))
        voice = data.get("voice", None)
        if not voice:
            character = character_registry.get(character_id)
            if character:
                voice = character.get("voice")

        return MessageParseResult(
            type="user_message",
            request=ChatRequest(
                text=user_text,
                user_id=authenticated_user_id,
                session_id=str(data.get("session_id", "default_session")),
                tts_enabled=bool(data.get("tts_enabled", True)),
                router_enabled=bool(data.get("router_enabled", settings.router_enabled)),
                character_id=character_id,
                voice=voice,
                turn_id=str(uuid4()),
            ),
        )

    return MessageParseResult(type=msg_type, error=f"Unknown message type: {msg_type}")
