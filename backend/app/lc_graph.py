"""LangGraph Orchestration for the chatbot pipeline."""

import asyncio
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from app.agents.base import AgentContext
from app.persona import build_system_prompt
from app.lc_chain import build_chain
from app.session_history import build_history_key
from app.tools import ToolInput, default_tool_registry


from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult


class TokenUsageCallbackHandler(AsyncCallbackHandler):
    def __init__(self):
        super().__init__()
        self.input_tokens = 0
        self.output_tokens = 0

    async def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        if response.llm_output:
            token_usage = response.llm_output.get("token_usage")
            if token_usage:
                self.input_tokens = token_usage.get("prompt_tokens", 0)
                self.output_tokens = token_usage.get("completion_tokens", 0)
        
        # Check generations usage metadata
        for generations in response.generations:
            for gen in generations:
                if hasattr(gen, "message") and hasattr(gen.message, "usage_metadata"):
                    meta = gen.message.usage_metadata
                    if meta:
                        if isinstance(meta, dict):
                            self.input_tokens = meta.get("input_tokens", self.input_tokens)
                            self.output_tokens = meta.get("output_tokens", self.output_tokens)
                        else:
                            self.input_tokens = getattr(meta, "input_tokens", self.input_tokens)
                            self.output_tokens = getattr(meta, "output_tokens", self.output_tokens)


class ChatState(TypedDict):
    user_id: str
    session_id: str
    character_id: str
    user_text: str
    provider: str | None         # set from the WebSocket "set_model" message
    selected_model: str | None   # set by HeuristicRouter in orchestrator.py
    memory_context: str | None
    character_context: str | None   # từ retrieve_character_context_node (character brain lore)
    system_prompt: str
    response_text: str
    emotion: str
    token_queue: asyncio.Queue
    turn_id: str | None
    agent_id: str | None


def _agent_context(state: ChatState) -> AgentContext:
    return AgentContext(
        user_id=state["user_id"],
        session_id=state["session_id"],
        character_id=state["character_id"],
        agent_id=state.get("agent_id"),
        provider=state.get("provider"),
        selected_model=state.get("selected_model"),
        turn_id=state.get("turn_id"),
    )


async def retrieve_memories_node(state: ChatState) -> dict:
    result = await default_tool_registry.run(
        ToolInput(
            name="retrieve_memory",
            args={"query": state["user_text"]},
            context=_agent_context(state),
        )
    )
    return {"memory_context": result.content if result.ok else ""}


async def retrieve_character_context_node(state: ChatState) -> dict:
    result = await default_tool_registry.run(
        ToolInput(
            name="retrieve_character_context",
            args={"query": state["user_text"]},
            context=_agent_context(state),
        )
    )
    return {"character_context": result.content if result.ok else ""}


async def build_prompt_node(state: ChatState) -> dict:
    system = build_system_prompt(
        state["character_id"], state.get("character_context"), state.get("memory_context")
    )
    return {"system_prompt": system}


async def generate_node(state: ChatState) -> dict:
    from app.orchestrator import _parse_emotion
    from app.feedback import FeedbackEvent, default_feedback_store
    from app.lc_chain import resolve_model_info

    q = state["token_queue"]
    full_response = ""
    handler = TokenUsageCallbackHandler()
    config = {
        "configurable": {
            "session_id": build_history_key(state["user_id"], state["character_id"], state["session_id"]),
        },
        "callbacks": [handler],
    }

    chain = build_chain(state.get("selected_model"), state.get("provider"))
    interrupted = False

    try:
        async for token in chain.astream(
            {"user_input": state["user_text"], "system_prompt": state["system_prompt"]},
            config=config,
        ):
            full_response += token
            await q.put(token)
    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        await q.put(None)  # sentinel for token stream

        input_tokens = handler.input_tokens
        output_tokens = handler.output_tokens

        # Fallback estimation if zero
        if input_tokens == 0:
            prompt_len = len(state["user_text"]) + len(state.get("system_prompt", ""))
            input_tokens = max(1, prompt_len // 4)
        if output_tokens == 0:
            output_tokens = max(1, len(full_response) // 4)

        info = resolve_model_info(state.get("selected_model"), state.get("provider"))
        cost_in = (input_tokens / 1_000_000) * info.cost_per_1m.get("input", 0.0)
        cost_out = (output_tokens / 1_000_000) * info.cost_per_1m.get("output", 0.0)
        total_cost = cost_in + cost_out

        if state.get("turn_id"):
            await default_feedback_store.record_event(
                FeedbackEvent(
                    event_type="token_usage",
                    user_id=state["user_id"],
                    session_id=state["session_id"],
                    turn_id=state["turn_id"],
                    payload={
                        "model_id": info.id,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "input_cost": cost_in,
                        "output_cost": cost_out,
                        "total_cost": total_cost,
                        "interrupted": interrupted,
                    }
                )
            )

    emotion, _ = _parse_emotion(full_response)
    return {"response_text": full_response, "emotion": emotion.value}


async def store_memories_node(state: ChatState) -> dict:
    await default_tool_registry.run(
        ToolInput(
            name="persist_memory",
            args={
                "user_text": state["user_text"],
                "assistant_text": state["response_text"],
                "emotion": state["emotion"],
            },
            context=_agent_context(state),
        )
    )
    return {}


builder = StateGraph(ChatState)
builder.add_node("retrieve_memories", retrieve_memories_node)
builder.add_node("retrieve_character_context", retrieve_character_context_node)
builder.add_node("build_prompt", build_prompt_node)
builder.add_node("generate", generate_node)
builder.add_node("store_memories", store_memories_node)

# retrieve_memories (turn history) and retrieve_character_context (lore) are
# independent reads — fan out from START, both must finish before build_prompt
# (LangGraph's superstep model runs sibling nodes concurrently and waits for
# all incoming edges before running build_prompt).
builder.add_edge(START, "retrieve_memories")
builder.add_edge(START, "retrieve_character_context")
builder.add_edge("retrieve_memories", "build_prompt")
builder.add_edge("retrieve_character_context", "build_prompt")
builder.add_edge("build_prompt", "generate")
builder.add_edge("generate", "store_memories")
builder.add_edge("store_memories", END)

graph = builder.compile()
