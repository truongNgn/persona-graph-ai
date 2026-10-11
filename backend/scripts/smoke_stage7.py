"""Stage 7 smoke: auth boundary, readiness, and persistent history."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.auth import issue_dev_token, resolve_auth_context
from app.gateway.schemas import parse_client_message
from app.main import app
from app.session_history import FileChatMessageHistory, build_history_key
from langchain_core.messages import HumanMessage


def main() -> None:
    token = issue_dev_token("smoke-user")
    auth = resolve_auth_context(authorization=f"Bearer {token}")
    assert auth.user_id == "smoke-user"

    parsed = parse_client_message(
        {
            "type": "user_message",
            "text": "hello",
            "user_id": "spoofed",
            "session_id": "smoke-session",
        },
        authenticated_user_id=auth.user_id,
    )
    assert parsed.request
    assert parsed.request.user_id == "smoke-user"

    history = FileChatMessageHistory(build_history_key("smoke-user", "default", "smoke-session"))
    history.add_message(HumanMessage(content="stage7 smoke"))
    assert history.messages[-1].content == "stage7 smoke"

    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["session_history"]["ready"] is True

    print("Stage 7 smoke passed.")


if __name__ == "__main__":
    main()
