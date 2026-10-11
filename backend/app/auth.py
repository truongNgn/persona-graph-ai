"""Minimal Stage 7 auth helpers.

This module intentionally avoids a new dependency: tokens are JWT-compatible
HS256 strings implemented with the Python standard library. Production can
swap this for python-jose/PyJWT without changing gateway call sites.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException, Query, Request, WebSocket, status

from app.config import settings


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    mode: str


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _json_b64(payload: dict[str, Any]) -> str:
    return _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def issue_dev_token(user_id: str | None = None) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id or settings.auth_dev_user_id,
        "iat": now,
        "exp": now + settings.auth_token_expire_minutes * 60,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_json_b64(header)}.{_json_b64(payload)}"
    signature = hmac.new(
        settings.auth_token_secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def verify_token(token: str) -> AuthContext:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(
            settings.auth_token_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied = _b64url_decode(signature_b64)
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid signature")

        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        if header.get("alg") != "HS256":
            raise ValueError("unsupported algorithm")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("token expired")
        user_id = str(payload.get("sub") or "").strip()
        if not user_id:
            raise ValueError("missing subject")
        return AuthContext(user_id=user_id, mode="jwt")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid auth token: {exc}",
        ) from exc


def _extract_bearer(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "bearer "
    if value.lower().startswith(prefix):
        return value[len(prefix):].strip()
    return value.strip()


def resolve_auth_context(authorization: str | None = None, token: str | None = None) -> AuthContext:
    """Resolve the caller's identity. A valid token is always required.

    Chat and conversation data are per-user, so there is no anonymous or
    shared "dev user" fallback: every request/connection must present a
    token issued by /api/auth/login, /api/auth/register, or the explicitly
    enabled local development login endpoint.
    """
    raw = token or _extract_bearer(authorization)
    if raw:
        return verify_token(raw)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")


async def get_request_auth_context(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> AuthContext:
    return resolve_auth_context(authorization=authorization, token=token)


def websocket_auth_context(websocket: WebSocket) -> AuthContext:
    return resolve_auth_context(
        authorization=websocket.headers.get("authorization"),
        token=websocket.query_params.get("token"),
    )


def request_client_key(request: Request, auth: AuthContext | None = None) -> str:
    if auth:
        return f"user:{auth.user_id}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"
