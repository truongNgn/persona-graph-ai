from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.auth import verify_token
from app.config import settings
from app.conversation_store import DEV_QUICK_LOGIN_USER_ID
from app.main import app


@pytest.mark.parametrize("mode,enabled", [("local", False), ("cloud", True), ("cloud", False)])
def test_dev_login_disabled_without_local_opt_in(monkeypatch, mode, enabled) -> None:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(settings, "dev_quick_login_enabled", enabled)
    create = AsyncMock()
    monkeypatch.setattr("app.main.get_or_create_dev_user", create)
    client = TestClient(app)
    assert client.get("/api/auth/dev-status").json() == {"enabled": False}
    assert client.post("/api/auth/dev-login").status_code == 404
    create.assert_not_called()


def test_dev_login_issues_normal_token_for_test_account(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "local")
    monkeypatch.setattr(settings, "dev_quick_login_enabled", True)
    user = SimpleNamespace(id=DEV_QUICK_LOGIN_USER_ID, email="dev-quick-login@localhost.invalid", display_name="Dev Tester", created_at=1.0)
    monkeypatch.setattr("app.main.get_or_create_dev_user", AsyncMock(return_value=user))
    client = TestClient(app)
    assert client.get("/api/auth/dev-status").json() == {"enabled": True}
    response = client.post("/api/auth/dev-login")
    assert response.status_code == 200
    payload = response.json()
    assert verify_token(payload["access_token"]).user_id == DEV_QUICK_LOGIN_USER_ID
    assert payload["user"]["display_name"] == "Dev Tester"
    assert "password_hash" not in payload["user"]


def test_dev_login_database_failure_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "local")
    monkeypatch.setattr(settings, "dev_quick_login_enabled", True)
    monkeypatch.setattr("app.main.get_or_create_dev_user", AsyncMock(side_effect=RuntimeError("database down")))
    response = TestClient(app).post("/api/auth/dev-login")
    assert response.status_code == 503
    assert response.json()["detail"] == "Development account is unavailable. Please retry."
