"""
Compatibility sentence producer.

This preserves the original Orchestrator.run() API used by older code while
delegating sentence-boundary behavior to app.orchestrator.streaming.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator

from app.agents import AgentContext, default_agent_registry
from app.config import settings
from app.feedback import FeedbackEvent, default_feedback_store
from app.lc_chain import normalize_provider
from app.model_registry import model_registry
from app.llm_handler import BaseLLMHandler
from app.models import SentenceChunk
from app.orchestrator.routing import HeuristicRouter, build_routing_context
from app.orchestrator.streaming import flush_buffer, should_flush

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        llm_handler: BaseLLMHandler | None = None,
        provider: str | None = None,
    ) -> None:
        # llm_handler là SDK thô, không tham gia sinh text (và không được
        # LangSmith trace). Đường sinh text là LangGraph → lc_chain.build_chain(),
        # nên provider mới là thứ phải truyền xuống. Giữ tham số cho tương thích
        # ngược, nhưng không tự dựng handler: provider chưa cấu hình key sẽ ném
        # lỗi ngay lúc khởi tạo và làm đứt WebSocket.
        self._llm = llm_handler
        self._provider = normalize_provider(provider)
        self._interrupted = False

    def interrupt(self) -> None:
        """Called when the gateway receives WebSocket event 'interrupt'."""
        self._interrupted = True
        logger.info("Orchestrator interrupted")

    def reset(self) -> None:
        self._interrupted = False

    async def run(
        self,
        user_text: str,
        session_id: str,
        user_id: str,
        sentence_queue: asyncio.Queue[SentenceChunk | None],
        voice: str | None = None,
        router_enabled: bool = True,
        character_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Run LangGraph -> sentence buffering -> queue."""
        self.reset()
        buffer = ""
        first_chunk = True
        agent_id = ""
        started_at = time.perf_counter()
        first_token_seen = False

        try:
            resolved_character_id = character_id or settings.default_character_id

            selected_model: str | None = None
            if router_enabled:
                large_info, small_info = model_registry.get_models_for_provider(self._provider)
                router = HeuristicRouter(
                    large_model=large_info.id,
                    small_model=small_info.id,
                )
                decision = router.select_model(build_routing_context(user_text))
                selected_model = decision.model

            agent = default_agent_registry.select(resolved_character_id)
            agent_id = agent.id
            agent_context = AgentContext(
                user_id=user_id,
                session_id=session_id,
                character_id=resolved_character_id,
                agent_id=agent_id,
                provider=self._provider,
                selected_model=selected_model,
                turn_id=turn_id,
            )
            logger.info(
                "Agent selected [turn=%s, session=%s, user=%s, character=%s, agent=%s, provider=%s, model=%s]",
                turn_id,
                session_id,
                user_id,
                resolved_character_id,
                agent_id,
                self._provider,
                selected_model,
            )
            if turn_id:
                await default_feedback_store.record_event(
                    FeedbackEvent(
                        event_type="agent_selected",
                        user_id=user_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        payload={
                            "agent_id": agent_id,
                            "provider": self._provider,
                            "selected_model": selected_model,
                            "router_enabled": router_enabled,
                            "character_id": resolved_character_id,
                        },
                    )
                )

            async for token in agent.stream(agent_context, user_text):
                if self._interrupted:
                    logger.info("Stream interrupted, stopping token consumption")
                    break
                if not first_token_seen:
                    first_token_seen = True
                    latency_ms = int((time.perf_counter() - started_at) * 1000)
                    logger.info(
                        "First token [turn=%s, session=%s, user=%s, agent=%s, selected_model=%s, latency_ms=%d]",
                        turn_id,
                        session_id,
                        user_id,
                        agent_id,
                        selected_model,
                        latency_ms,
                    )
                    if turn_id:
                        await default_feedback_store.record_event(
                            FeedbackEvent(
                                event_type="first_token",
                                user_id=user_id,
                                session_id=session_id,
                                turn_id=turn_id,
                                payload={
                                    "latency_ms": latency_ms,
                                    "agent_id": agent_id,
                                    "provider": self._provider,
                                    "selected_model": selected_model,
                                },
                            )
                        )

                buffer += token

                if buffer and should_flush(buffer, buffer[-1], is_first_chunk=first_chunk):
                    chunk = flush_buffer(buffer, voice)
                    if chunk:
                        logger.debug(
                            "Flushed chunk [first=%s turn=%s]: emotion=%s len=%d text=%r voice=%s",
                            first_chunk,
                            turn_id,
                            chunk.emotion,
                            len(chunk.text),
                            chunk.text[:60],
                            chunk.voice,
                        )
                        await sentence_queue.put(chunk)
                        first_chunk = False
                    buffer = ""

            if buffer.strip() and not self._interrupted:
                chunk = flush_buffer(buffer, voice)
                if chunk:
                    logger.debug(
                        "Final flush [turn=%s]: emotion=%s text=%r voice=%s",
                        turn_id,
                        chunk.emotion,
                        chunk.text[:60],
                        chunk.voice,
                    )
                    await sentence_queue.put(chunk)

        except Exception as exc:
            logger.error("Orchestrator error [turn=%s agent=%s]: %s", turn_id, agent_id, exc)
            raise
        finally:
            await sentence_queue.put(None)


async def sentence_stream(
    user_text: str,
    sentence_queue: asyncio.Queue[SentenceChunk | None],
) -> AsyncGenerator[SentenceChunk, None]:
    orchestrator = Orchestrator()
    producer_task = asyncio.create_task(
        orchestrator.run(
            user_text,
            "default_session",
            "default_user",
            sentence_queue,
        )
    )
    while True:
        item = await sentence_queue.get()
        if item is None:
            break
        yield item
    await producer_task
