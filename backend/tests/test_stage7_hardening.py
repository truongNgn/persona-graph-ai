import time

from langchain_core.messages import AIMessage, HumanMessage

from app.auth import issue_dev_token, resolve_auth_context
from app.gateway.schemas import parse_client_message
from app.session_history import FileChatMessageHistory, build_history_key


def test_gateway_uses_authenticated_user_over_payload() -> None:
    parsed = parse_client_message(
        {
            "type": "user_message",
            "text": "hello",
            "user_id": "spoofed-user",
            "session_id": "s1",
        },
        authenticated_user_id="real-user",
    )

    assert parsed.request is not None
    assert parsed.request.user_id == "real-user"


def test_dev_token_roundtrip() -> None:
    token = issue_dev_token("stage7-user")
    auth = resolve_auth_context(authorization=f"Bearer {token}")

    assert auth.user_id == "stage7-user"
    assert auth.mode == "jwt"


def test_file_chat_history_persists_messages(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.session_history.settings.session_history_path", str(tmp_path))
    key = build_history_key("u1", "default", "s1")

    first = FileChatMessageHistory(key)
    first.add_messages([HumanMessage(content="hello"), AIMessage(content="hi")])

    second = FileChatMessageHistory(key)
    assert [message.content for message in second.messages] == ["hello", "hi"]


def test_expired_token_rejected(monkeypatch) -> None:
    monkeypatch.setattr("app.auth.settings.auth_token_expire_minutes", 0)
    token = issue_dev_token("expired-user")
    time.sleep(1)

    try:
        resolve_auth_context(token=token)
    except Exception as exc:
        assert "401" in repr(exc) or "Invalid auth token" in str(exc)
    else:
        raise AssertionError("expired token should fail")
