"""Admin access, API contract, and confidential Langfuse proxy behavior."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import admin_monitoring
from app.auth import issue_dev_token
from app.config import settings
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "admin_user_ids", "admin-test")
    monkeypatch.setattr(settings, "admin_emails", "")
    monkeypatch.setattr(settings, "admin_local_emails", "")
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-confidential")
    monkeypatch.setattr(settings, "deployment_mode", "local")
    monkeypatch.setattr(admin_monitoring, "get_user", AsyncMock(return_value=SimpleNamespace(email="owner@example.com")))
    return TestClient(app)


def headers(user: str = "admin-test") -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_dev_token(user)}"}


@pytest.mark.parametrize("path", ["/dashboard", "/generations", "/generations/test-id"])
def test_monitoring_requires_admin_on_every_data_route(client, path):
    assert client.get("/api/admin/monitoring" + path).status_code == 401
    assert client.get("/api/admin/monitoring" + path, headers=headers("regular-user")).status_code == 403


def test_empty_allowlist_denies_everyone_and_access_probe_hides_settings(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_user_ids", "")
    response = client.get("/api/admin/monitoring/access", headers=headers())
    assert response.json() == {"allowed": False}
    assert response.headers["cache-control"] == "no-store"


def test_cloud_admin_cannot_be_promoted_by_an_unverified_email(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "admin_user_ids", "")
    monkeypatch.setattr(settings, "admin_emails", "owner@example.com")
    assert client.get("/api/admin/monitoring/dashboard", headers=headers()).status_code == 403


def test_pinned_cloud_admin_requires_an_existing_account(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    assert client.get("/api/admin/monitoring/access", headers=headers()).json()["allowed"]
    monkeypatch.setattr(admin_monitoring, "get_user", AsyncMock(return_value=None))
    assert client.get("/api/admin/monitoring/dashboard", headers=headers()).status_code == 403


@pytest.mark.parametrize("url", ["http://cloud.langfuse.com", "https://user:password@cloud.langfuse.com", "https://cloud.langfuse.com?key=private"])
def test_cloud_endpoint_rejects_plaintext_or_embedded_credentials(client, monkeypatch, url):
    monkeypatch.setattr(settings, "deployment_mode", "cloud")
    monkeypatch.setattr(settings, "langfuse_base_url", url)
    response = client.get("/api/admin/monitoring/dashboard", headers=headers())
    assert response.status_code == 503
    assert url not in response.text


@pytest.mark.parametrize("mode,expected", [("local", True), ("cloud", False)])
def test_local_admin_email_is_restricted_to_local_mode(client, monkeypatch, mode, expected):
    monkeypatch.setattr(settings, "admin_user_ids", "")
    monkeypatch.setattr(settings, "admin_local_emails", " admin@gmail.com ")
    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(admin_monitoring, "get_user", AsyncMock(return_value=SimpleNamespace(email="ADMIN@gmail.com")))
    assert client.get("/api/admin/monitoring/access", headers=headers()).json() == {"allowed": expected}


def test_main_admin_email_and_database_failure(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_user_ids", "")
    monkeypatch.setattr(settings, "admin_emails", "owner@example.com")
    monkeypatch.setattr(admin_monitoring, "get_user", AsyncMock(return_value=SimpleNamespace(email="owner@example.com")))
    assert client.get("/api/admin/monitoring/access", headers=headers()).json()["allowed"]
    monkeypatch.setattr(admin_monitoring, "get_user", AsyncMock(side_effect=RuntimeError("private DB connection")))
    response = client.get("/api/admin/monitoring/dashboard", headers=headers())
    assert response.status_code == 503
    assert "private DB" not in response.text


def test_missing_langfuse_keys_is_explicit_empty_state(client, monkeypatch):
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    response = client.get("/api/admin/monitoring/dashboard", headers=headers())
    assert response.status_code == 200
    assert response.json()["status"] == "unconfigured"
    assert response.json()["generations"] == []
    assert "secret_key" not in response.text


def install_transport(monkeypatch, handler):
    # Capture the class before replacing the factory to avoid patching httpx itself.
    async_client = httpx.AsyncClient
    monkeypatch.setattr(admin_monitoring, "langfuse_client", lambda: async_client(
        base_url="https://langfuse.test/", auth=httpx.BasicAuth("pk-lf-test", "sk-lf-confidential"),
        transport=httpx.MockTransport(handler),
    ))


def test_dashboard_uses_aggregates_and_scopes_every_query(client, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers.get("authorization", "").startswith("Basic ")
        if request.url.path.endswith("metrics"):
            query = json.loads(request.url.params["query"])
            assert query["view"] == "observations"
            assert {item["column"]: item["value"] for item in query["filters"]} == {
                "type": "GENERATION", "traceName": "interactive_chatbot_turn",
                "environment": settings.langfuse_environment, "providedModelName": "persona-qwen",
            }
            return httpx.Response(200, json={"data": [{"count_count": 900, "sum_totalCost": 1.2, "avg_latency": 2000}]})
        assert "io" not in request.url.params["fields"].split(",")
        assert "filter" in request.url.params
        return httpx.Response(200, json={"data": [{"id": "generation-1", "totalCost": 0.1}], "meta": {"cursor": "next-page"}})

    install_transport(monkeypatch, respond)
    response = client.get("/api/admin/monitoring/dashboard?days=7&model=persona-qwen", headers=headers())
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["count_count"] == 900  # whole-period totals, not 50-row page
    assert body["cursor"] == "next-page"
    assert body["status"] == "connected"
    assert len(requests) == 4
    assert response.headers["cache-control"] == "no-store"
    assert "sk-lf-confidential" not in response.text


def test_cursor_and_time_window_are_preserved(client, monkeypatch):
    from datetime import datetime, timezone
    end = datetime.now(timezone.utc).isoformat()

    def respond(request):
        assert request.url.params["cursor"] == "next-page"
        query = json.loads(request.url.params["filter"])
        assert next(item["value"] for item in query if item["column"] == "startTime" and item["operator"] == "<") == end
        return httpx.Response(200, json={"data": [], "meta": {"cursor": None}})

    install_transport(monkeypatch, respond)
    response = client.get("/api/admin/monitoring/generations", params={"cursor": "next-page", "days": 7, "end_at": end}, headers=headers())
    assert response.status_code == 200
    assert response.json() == {"generations": [], "cursor": None}
    assert client.get("/api/admin/monitoring/dashboard?days=99", headers=headers()).status_code == 422
    assert client.get("/api/admin/monitoring/generations?end_at=2026-01-01T00:00:00", headers=headers()).status_code == 422


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_upstream_errors_do_not_expose_keys_or_response_body(client, monkeypatch, status):
    install_transport(monkeypatch, lambda request: httpx.Response(status, text="sk-lf-confidential provider stack trace"))
    response = client.get("/api/admin/monitoring/dashboard", headers=headers())
    assert response.status_code == 502
    assert "sk-lf-confidential" not in response.text
    assert "stack trace" not in response.text


def test_detail_is_scoped_and_includes_io_only_on_demand(client, monkeypatch):
    def respond(request):
        query = json.loads(request.url.params["filter"])
        assert {item["column"]: item["value"] for item in query}["id"] == "generation-1"
        assert "io" in request.url.params["fields"].split(",")
        return httpx.Response(200, json={"data": [{"id": "generation-1", "input": "hello", "output": "world"}]})

    install_transport(monkeypatch, respond)
    response = client.get("/api/admin/monitoring/generations/generation-1", headers=headers())
    assert response.status_code == 200
    assert response.json()["output"] == "world"


def test_detail_not_found_and_network_error(client, monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
    assert client.get("/api/admin/monitoring/generations/missing", headers=headers()).status_code == 404

    def timeout(request):
        raise httpx.ReadTimeout("private upstream", request=request)

    install_transport(monkeypatch, timeout)
    response = client.get("/api/admin/monitoring/dashboard", headers=headers())
    assert response.status_code == 502
    assert "private upstream" not in response.text
