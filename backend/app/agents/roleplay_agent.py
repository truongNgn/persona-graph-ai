"""Roleplay chat agent wrapping the existing LangGraph pipeline."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.agents.base import AgentContext
from app.observability import langfuse_callbacks, langfuse_metadata

logger = logging.getLogger(__name__)


class RoleplayChatAgent:
    id = "roleplay_chat"

    async def stream(self, context: AgentContext, user_text: str) -> AsyncIterator[str]:
        from app.lc_graph import graph

        token_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def invoke_graph(state: dict, config: dict) -> None:
            try:
                await graph.ainvoke(state, config=config)
            finally:
                # Also terminate consumers when retrieval/model setup fails before
                # generate_node can send its own sentinel.
                await token_queue.put(None)

        graph_task = asyncio.create_task(
            invoke_graph(
                {
                    "user_id": context.user_id,
                    "session_id": context.session_id,
                    "character_id": context.character_id,
                    "user_text": user_text,
                    "provider": context.provider,
                    "selected_model": context.selected_model,
                    "token_queue": token_queue,
                    "turn_id": context.turn_id,
                    "agent_id": self.id,
                },
                config={
                    "run_name": "interactive_chatbot_turn",
                    "callbacks": langfuse_callbacks(),
                    "tags": [
                        "websocket",
                        "langgraph",
                        "streaming",
                        f"agent:{self.id}",
                        f"provider:{context.provider}",
                    ],
                    "metadata": {
                        **langfuse_metadata(context.user_id, context.character_id, context.session_id),
                        "turn_id": context.turn_id,
                        "session_id": context.session_id,
                        "user_id": context.user_id,
                        "character_id": context.character_id,
                        "provider": context.provider,
                        "selected_model": context.selected_model,
                        "agent_id": self.id,
                    },
                },
            )
        )

        try:
            while True:
                token = await token_queue.get()
                if token is None:
                    break
                yield token
            await graph_task
        finally:
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except asyncio.CancelledError:
                    pass
