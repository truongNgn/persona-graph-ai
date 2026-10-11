"""WebSocket gateway for the existing /ws/chat contract."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.auth import websocket_auth_context
from app.config import settings
from app.model_availability import is_model_available
from app.gateway.schemas import ChatRequest, parse_client_message
from app.models import ErrorPayload
from app.orchestrator import Orchestrator, TurnOrchestrator
from app.rate_limit import allow_ws_message
from app.tts_handler import BaseTTSHandler
from app.warmup import warmup_state

logger = logging.getLogger(__name__)


class WebSocketSink:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_text(self, text: str) -> None:
        await self._websocket.send_text(text)


def _build_turn_orchestrator(tts: BaseTTSHandler, provider: str) -> TurnOrchestrator:
    return TurnOrchestrator(
        tts_handler=tts,
        sentence_producer=Orchestrator(provider=provider),
    )


async def websocket_chat(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        auth_context = websocket_auth_context(websocket)
    except Exception as exc:
        logger.warning("WebSocket authentication failed: %s", exc)
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Authentication failed. Please log in again."
        }))
        await websocket.close(code=1008)
        return

    client = websocket.client
    logger.info("WebSocket connected: %s user=%s auth=%s", client, auth_context.user_id, auth_context.mode)

    tts: BaseTTSHandler = websocket.app.state.tts_handler
    current_provider = settings.llm_provider
    try:
        turn_orchestrator = _build_turn_orchestrator(tts, current_provider)
    except Exception as exc:
        # A misconfigured provider (e.g. missing API key) must not tear the
        # socket down with an unhandled error — the client only sees a generic
        # handshake failure. Report it, then close cleanly.
        logger.error("LLM provider '%s' unavailable: %s", current_provider, exc)
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": f"LLM provider '{current_provider}' is not configured on the server.",
        }))
        await websocket.close(code=1011)
        return

    current_task: asyncio.Task | None = None
    sink = WebSocketSink(websocket)

    await websocket.send_text(json.dumps({
        "type": "connected",
        "provider": current_provider,
        "warmup": warmup_state.to_dict(),
        "auth": {"mode": auth_context.mode, "user_id": auth_context.user_id},
    }))

    async def _cancel_current() -> None:
        nonlocal current_task
        if current_task and not current_task.done():
            turn_orchestrator.interrupt()
            current_task.cancel()
            try:
                await current_task
            except (asyncio.CancelledError, Exception):
                pass
        turn_orchestrator.reset()

    async def _run_request(request: ChatRequest) -> None:
        logger.info(
            "New message from %s [turn=%s, session=%s, user=%s, character=%s, tts=%s, router=%s, voice=%s]: %r",
            client,
            request.turn_id,
            request.session_id,
            request.user_id,
            request.character_id,
            request.tts_enabled,
            request.router_enabled,
            request.voice,
            request.text[:80],
        )
        await turn_orchestrator.run_turn(request, sink)

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > settings.max_ws_message_bytes:
                await websocket.send_text(ErrorPayload(message="WebSocket message too large.").model_dump_json())
                continue
            if not allow_ws_message(websocket, auth_context.user_id):
                await websocket.send_text(ErrorPayload(message="Rate limit exceeded.").model_dump_json())
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(ErrorPayload(message="Invalid JSON").model_dump_json())
                continue

            message = parse_client_message(data, authenticated_user_id=auth_context.user_id)

            if message.error:
                await websocket.send_text(ErrorPayload(message=message.error).model_dump_json())
                continue

            if message.type == "interrupt":
                logger.info("Interrupt signal from %s", client)
                await _cancel_current()
                await websocket.send_text(json.dumps({"type": "clear_queue"}))

            elif message.type == "set_model":
                await _cancel_current()
                model_or_provider = message.provider or settings.llm_provider
                
                from app.model_registry import model_registry
                
                try:
                    large_info, small_info = model_registry.get_models_for_provider(model_or_provider)
                except Exception as exc:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Model or provider '{model_or_provider}' is not registered."
                    }))
                    continue

                error_msg = None
                # Validate both models (large and small) for the selected option
                for info in (large_info, small_info):
                    # Check 1: deployment mode compatibility
                    if settings.deployment_mode == "cloud" and info.deployment == "local":
                        error_msg = f"Model '{info.id}' is only available locally, not in cloud mode."
                        break

                    # Check 2: required credentials
                    for req in info.requires:
                        if not getattr(settings, req, None):
                            error_msg = f"Missing API key '{req}' for model '{info.id}'."
                            break
                    if error_msg:
                        break

                    # Check 3: reachability ping
                    reachable = await is_model_available(info)

                    if not reachable:
                        error_msg = f"Model '{info.model}' is not served by its runtime endpoint."
                        break

                if error_msg:
                    logger.warning("Preflight failed for '%s': %s", model_or_provider, error_msg)
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Cannot switch model: {error_msg}"
                    }))
                    continue

                current_provider = model_or_provider
                turn_orchestrator = _build_turn_orchestrator(tts, current_provider)
                logger.info("LLM provider switched to '%s' for %s", current_provider, client)
                await websocket.send_text(json.dumps({
                    "type": "model_changed",
                    "provider": current_provider,
                }))

            elif message.type == "user_message" and message.request:
                await _cancel_current()
                current_task = asyncio.create_task(_run_request(message.request))

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", client)
        await _cancel_current()
    except Exception as exc:
        logger.error("Unexpected WebSocket error: %s", exc)
        await _cancel_current()
