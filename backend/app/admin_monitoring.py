"""Read-only admin monitoring API; Langfuse credentials never reach the browser."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.auth import AuthContext, get_request_auth_context
from app.config import settings, validated_langfuse_base_url
from app.conversation_store import get_user

router = APIRouter(prefix="/api/admin/monitoring", tags=["Admin monitoring"])


async def is_admin(auth: AuthContext) -> bool:
    ids = {value.strip() for value in settings.admin_user_ids.split(",") if value.strip()}
    # Signup does not verify email ownership. Cloud admins must be pinned to
    # an existing account ID, never promoted by merely claiming an email.
    emails: set[str] = set()
    if settings.deployment_mode == "local":
        emails.update(value.strip().casefold() for value in settings.admin_emails.split(",") if value.strip())
        emails.update(value.strip().casefold() for value in settings.admin_local_emails.split(",") if value.strip())
    if auth.user_id not in ids and not emails:
        return False
    try:
        user = await get_user(auth.user_id)
    except Exception as exc:
        raise HTTPException(503, "Admin account verification is temporarily unavailable.") from exc
    return bool(user and (auth.user_id in ids or user.email.casefold() in emails))


async def require_admin(auth: AuthContext = Depends(get_request_auth_context)) -> AuthContext:
    if not await is_admin(auth):
        raise HTTPException(403, "Administrator access required.")
    return auth


def connection_status() -> str:
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return "unconfigured"
    return "configured"


@router.get("/access")
async def access(response: Response, auth: AuthContext = Depends(get_request_auth_context)) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    return {"allowed": await is_admin(auth)}


def window(days: int, end_at: datetime | None = None) -> tuple[str, str]:
    end = end_at or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise HTTPException(422, "end_at must include a timezone.")
    now = datetime.now(timezone.utc)
    if end > now + timedelta(minutes=1) or end < now - timedelta(days=31):
        raise HTTPException(422, "end_at must be within the last 31 days.")
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def filters(model: str | None = None, *, observations: bool = False) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = [
        {"type": "string", "column": "type", "operator": "=", "value": "GENERATION"},
        {"type": "string", "column": "traceName", "operator": "=", "value": "interactive_chatbot_turn"},
        {"type": "string", "column": "environment", "operator": "=", "value": settings.langfuse_environment},
    ]
    if model:
        values.append({"type": "string", "column": "model" if observations else "providedModelName", "operator": "=", "value": model})
    return values


async def request_langfuse(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await client.get(path, params=params)
        result.raise_for_status()
        payload = result.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("invalid response")
        return payload
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            message = "Langfuse rejected the project credentials. Check the keys and instance URL."
        elif exc.response.status_code == 429:
            message = "Langfuse rate limit reached. Wait briefly and refresh."
        else:
            message = "Langfuse monitoring API is unavailable. This dashboard requires Langfuse v4/v2 read APIs."
        raise HTTPException(502, message) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, "Cannot read Langfuse data. Check connectivity and the instance URL.") from exc


def metrics_query(start: str, end: str, model: str | None, dimensions: list[str], trend: bool = False) -> str:
    measures = [("count", "count"), ("totalCost", "sum"), ("inputTokens", "sum"),
                ("outputTokens", "sum"), ("latency", "avg"), ("timeToFirstToken", "avg")]
    query: dict[str, Any] = {
        "view": "observations", "dimensions": [{"field": dim} for dim in dimensions],
        "metrics": [{"measure": measure, "aggregation": aggregation} for measure, aggregation in measures],
        "filters": filters(model), "fromTimestamp": start, "toTimestamp": end,
        "config": {"row_limit": 1000},
    }
    if trend:
        query["timeDimension"] = {"granularity": "day"}
    return json.dumps(query)


def observations_query(start: str, end: str, model: str | None, cursor: str | None = None) -> dict[str, Any]:
    conditions = filters(model, observations=True) + [
        {"type": "datetime", "column": "startTime", "operator": ">=", "value": start},
        {"type": "datetime", "column": "startTime", "operator": "<", "value": end},
    ]
    params = {"filter": json.dumps(conditions), "fields": "basic,model,usage,metrics,trace_context", "limit": 50}
    if cursor:
        params["cursor"] = cursor
    return params


def langfuse_client() -> httpx.AsyncClient:
    try:
        base_url = validated_langfuse_base_url()
    except ValueError as exc:
        raise HTTPException(503, "Invalid Langfuse endpoint configuration; cloud connections require HTTPS.") from exc
    return httpx.AsyncClient(
        base_url=base_url + "/",
        auth=httpx.BasicAuth(settings.langfuse_public_key, settings.langfuse_secret_key),
        timeout=httpx.Timeout(15.0), follow_redirects=False,
    )


@router.get("/dashboard", dependencies=[Depends(require_admin)])
async def dashboard(response: Response, days: int = Query(7, ge=1, le=30), model: str | None = Query(None, max_length=128)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    start, end = window(days)
    base = {"status": connection_status(), "tracing_enabled": settings.langfuse_enabled,
            "environment": settings.langfuse_environment, "from_time": start, "to_time": end}
    if base["status"] == "unconfigured":
        return {**base, "summary": {}, "models": [], "trend": [], "generations": [], "cursor": None}
    async with langfuse_client() as client:
        summary, models, trend, observations = await asyncio.gather(
            request_langfuse(client, "api/public/v2/metrics", {"query": metrics_query(start, end, model, [])}),
            request_langfuse(client, "api/public/v2/metrics", {"query": metrics_query(start, end, model, ["providedModelName", "level"])}),
            request_langfuse(client, "api/public/v2/metrics", {"query": metrics_query(start, end, model, [], trend=True)}),
            request_langfuse(client, "api/public/v2/observations", observations_query(start, end, model)),
        )
    return {**base, "status": "connected", "summary": summary["data"][0] if summary["data"] else {},
            "models": models["data"], "trend": trend["data"], "generations": observations["data"],
            "cursor": observations.get("meta", {}).get("cursor")}


@router.get("/generations", dependencies=[Depends(require_admin)])
async def generations(response: Response, days: int = Query(7, ge=1, le=30),
                      end_at: datetime | None = None, model: str | None = Query(None, max_length=128),
                      cursor: str | None = Query(None, max_length=4096)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if connection_status() == "unconfigured":
        raise HTTPException(503, "Langfuse project keys are not configured.")
    start, end = window(days, end_at)
    async with langfuse_client() as client:
        payload = await request_langfuse(client, "api/public/v2/observations", observations_query(start, end, model, cursor))
    return {"generations": payload["data"], "cursor": payload.get("meta", {}).get("cursor")}


@router.get("/generations/{observation_id}", dependencies=[Depends(require_admin)])
async def generation_detail(observation_id: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if connection_status() == "unconfigured":
        raise HTTPException(503, "Langfuse project keys are not configured.")
    if len(observation_id) > 128:
        raise HTTPException(422, "Invalid observation ID.")
    conditions = filters(observations=True) + [{"type": "string", "column": "id", "operator": "=", "value": observation_id}]
    async with langfuse_client() as client:
        payload = await request_langfuse(client, "api/public/v2/observations", {
            "filter": json.dumps(conditions), "fields": "basic,model,usage,metrics,trace_context,io,metadata", "limit": 1,
        })
    if not payload["data"]:
        raise HTTPException(404, "Generation not found in this environment.")
    return payload["data"][0]
