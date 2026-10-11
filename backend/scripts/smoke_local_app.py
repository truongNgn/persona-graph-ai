"""Opt-in dev-login smoke test through the frontend proxy; deletes test chats.

Run from backend: .venv/Scripts/python.exe scripts/smoke_local_app.py
Requires local DEV_QUICK_LOGIN_ENABLED and the running Compose app.
"""
from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import httpx
import websockets


async def main() -> None:
    async with httpx.AsyncClient(base_url="http://localhost:8088", timeout=120) as client:
        login = await client.post("/api/auth/dev-login")
        login.raise_for_status()
        identity = login.json()
        client.headers["Authorization"] = f"Bearer {identity['access_token']}"
        catalog = await client.get("/api/llm/models")
        catalog.raise_for_status()
        assert catalog.json()["default"] == "local-large"
        for voice in (False, True):
            session = f"llama-smoke-{uuid4()}"
            try:
                async with websockets.connect(
                    f"ws://localhost:8088/ws/chat?token={identity['access_token']}",
                    max_size=16 * 1024 * 1024,
                ) as socket:
                    connected = json.loads(await socket.recv())
                    assert connected["type"] == "connected"
                    assert connected["provider"] == "llama_cpp"
                    # Exercise both the new selection and a saved legacy model ID.
                    await socket.send(json.dumps({"type": "set_model", "provider": "vllm-small" if voice else "local-large"}))
                    changed = json.loads(await socket.recv())
                    assert changed["type"] == "model_changed", changed
                    started = time.perf_counter()
                    await socket.send(json.dumps({
                        "type": "user_message", "text": "Hello! Please greet me in one short sentence.",
                        "session_id": session, "character_id": "luna",
                        "tts_enabled": voice, "voice": "af_bella",
                    }))
                    text = ""
                    audio = False
                    first: float | None = None
                    async with asyncio.timeout(120):
                        while True:
                            event = json.loads(await socket.recv())
                            assert event["type"] != "error", event
                            if event["type"] == "audio_chunk":
                                text += event["text"]
                                audio = audio or bool(event.get("audio_base64"))
                                first = first or time.perf_counter()
                            if event["type"] == "done":
                                break
                    assert text and (audio if voice else not audio)
                    # Done precedes the final DB write; allow it to finish before closing.
                    await asyncio.sleep(1)
                detail = await client.get(f"/api/conversations/{session}")
                detail.raise_for_status()
                assert len(detail.json()["messages"]) >= 2
                print(json.dumps({
                    "tts": voice, "first_response_ms": round((first - started) * 1000),
                    "text_chars": len(text), "audio_received": audio, "persistence": "OK",
                    "session": session, "user_id": identity["user"]["id"],
                }))
            finally:
                deleted = await client.delete(f"/api/conversations/{session}")
                assert deleted.status_code in (200, 404), deleted.text


if __name__ == "__main__":
    asyncio.run(main())
