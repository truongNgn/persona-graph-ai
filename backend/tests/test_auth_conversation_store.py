from app.auth import issue_dev_token, resolve_auth_context
from app.conversation_store import hash_password, verify_password


def test_password_hash_roundtrip() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong password", password_hash)


def test_issued_auth_token_resolves_user() -> None:
    token = issue_dev_token("auth-user")
    auth = resolve_auth_context(authorization=f"Bearer {token}")

    assert auth.user_id == "auth-user"
    assert auth.mode == "jwt"


def test_append_message_records_timestamp(monkeypatch) -> None:
    import asyncio
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import conversation_store

    rows = []
    conversation = SimpleNamespace(id="test-session", updated_at=0)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def begin(self):
            return self

        def add(self, row):
            rows.append(row)

    monkeypatch.setattr(conversation_store, "async_session_factory", Session)
    monkeypatch.setattr(conversation_store, "ensure_conversation", AsyncMock(return_value=conversation))
    started = time.time()
    asyncio.run(conversation_store.append_message(
        user_id="test-user", conversation_id="test-session", character_id="luna",
        role="human", content="Hello",
    ))
    assert len(rows) == 1
    assert rows[0].created_at >= started
    assert conversation.updated_at == rows[0].created_at
