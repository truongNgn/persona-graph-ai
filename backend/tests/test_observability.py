"""Tracing must preserve streamed chat, error propagation, and turn isolation."""

import asyncio
from types import SimpleNamespace

import pytest

from app import observability
from app.agents.base import AgentContext
from app.agents.roleplay_agent import RoleplayChatAgent


@pytest.fixture(autouse=True)
def isolated_tracing(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr(observability, "_initialization_attempted", False)
    monkeypatch.setattr(observability.settings, "langfuse_enabled", False)


def test_disabled_and_missing_keys_do_not_create_client(monkeypatch):
    assert observability.langfuse_callbacks() == []
    monkeypatch.setattr(observability.settings, "langfuse_enabled", True)
    monkeypatch.setattr(observability.settings, "langfuse_public_key", "")
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", "")
    assert observability.langfuse_callbacks() == []


def test_failed_initialization_keeps_chat_available(monkeypatch):
    import langfuse

    monkeypatch.setattr(observability.settings, "langfuse_enabled", True)
    monkeypatch.setattr(observability.settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", "sk-lf-test")

    def fail(**kwargs):
        raise RuntimeError("exporter setup failed")

    monkeypatch.setattr(langfuse, "Langfuse", fail)
    assert observability.langfuse_callbacks() == []
    assert observability.langfuse_callbacks() == []


def test_real_sdk_records_usage_errors_and_isolates_concurrent_turns(monkeypatch):
    from langfuse import Langfuse
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-offline-observability-test",
        secret_key="sk-lf-offline-test",
        span_exporter=exporter,
        environment="test",
    )
    monkeypatch.setattr(observability.settings, "langfuse_enabled", True)
    monkeypatch.setattr(observability.settings, "langfuse_public_key", "pk-lf-offline-observability-test")
    monkeypatch.setattr(observability, "_client", client)
    monkeypatch.setattr(observability, "_initialization_attempted", True)

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    usage_model = FakeMessagesListChatModel(responses=[AIMessage(
        content="hello", usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
    )])

    async def run_turn(user: str):
        config = {
            "callbacks": observability.langfuse_callbacks(),
            "metadata": observability.langfuse_metadata(user, "luna", "shared-session"),
            "run_name": "interactive_chatbot_turn",
        }
        assert len(config["callbacks"]) == 1
        stream = FakeListChatModel(responses=["hello"])
        chunks = [chunk.content async for chunk in stream.astream("prompt", config=config)]
        assert "".join(chunks) == "hello"
        await usage_model.ainvoke("prompt", config=config)

        def fail(_):
            raise RuntimeError("test model unavailable")

        fallback = RunnableLambda(fail).with_fallbacks([stream])
        assert (await fallback.ainvoke("prompt", config=config)).content == "hello"

    async def run():
        await asyncio.gather(run_turn("alice"), run_turn("bob"))

    try:
        asyncio.run(run())
        client.flush()
        spans = exporter.get_finished_spans()
        assert spans
        sessions = {s.attributes.get("session.id") for s in spans}
        assert {"alice:luna:shared-session", "bob:luna:shared-session"} <= sessions
        assert any(s.attributes.get("langfuse.observation.usage_details") for s in spans)
        assert any(s.attributes.get("langfuse.observation.level") == "ERROR" for s in spans)
        for trace_id in {s.context.trace_id for s in spans}:
            users = {s.attributes.get("user.id") for s in spans if s.context.trace_id == trace_id}
            assert not {"alice", "bob"} <= users
    finally:
        asyncio.run(observability.shutdown_langfuse())


@pytest.mark.parametrize("failure", [False, True])
def test_agent_wires_graph_tracing_and_unblocks_on_early_failure(monkeypatch, failure):
    from app import lc_graph
    from app.agents import roleplay_agent

    seen = []
    callback = object()
    monkeypatch.setattr(roleplay_agent, "langfuse_callbacks", lambda: [callback])

    async def invoke(state, config):
        seen.append(config)
        if failure:
            raise RuntimeError("retrieval failed")
        await state["token_queue"].put("hello")

    monkeypatch.setattr(lc_graph, "graph", SimpleNamespace(ainvoke=invoke))
    context = AgentContext(user_id="alice", session_id="session", character_id="luna")

    async def run():
        return [token async for token in RoleplayChatAgent().stream(context, "hi")]

    if failure:
        with pytest.raises(RuntimeError, match="retrieval failed"):
            asyncio.run(asyncio.wait_for(run(), timeout=2))
    else:
        assert asyncio.run(run()) == ["hello"]
    assert seen[0]["callbacks"] == [callback]
    assert seen[0]["metadata"]["langfuse_session_id"] == "alice:luna:session"


def test_agent_cancellation_stops_graph_task(monkeypatch):
    from app import lc_graph

    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def invoke(state, config):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(lc_graph, "graph", SimpleNamespace(ainvoke=invoke))
        context = AgentContext(user_id="alice", session_id="session", character_id="luna")
        iterator = RoleplayChatAgent().stream(context, "hi")
        task = asyncio.create_task(anext(iterator))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(run())
