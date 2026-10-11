"""Check local inference + embeddings and measure already-loaded streaming.

Run from backend: .venv/Scripts/python.exe scripts/smoke_local_llm.py
This script does not create application conversations or memories.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.llm_handler import get_llm_handler


async def main() -> None:
    assert await get_llm_handler().health_check(), "Configured model is not served"
    async with httpx.AsyncClient(timeout=120.0) as client:
        for run in range(2):
            started = time.perf_counter()
            first: float | None = None
            count = 0
            result = ""
            async with client.stream(
                "POST", f"{settings.local_llm_base_url}/chat/completions",
                json={
                    "model": settings.local_llm_large_model,
                    "messages": [
                        {"role": "system", "content": "You are a helpful, concise assistant."},
                        {"role": "user", "content": "Explain streaming in a conversational app in a short paragraph."},
                    ],
                    "temperature": 0, "max_tokens": 64, "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    item = json.loads(line[6:])
                    if item.get("usage"):
                        count = item["usage"]["completion_tokens"]
                    for choice in item.get("choices", []):
                        token = choice.get("delta", {}).get("content")
                        if token:
                            first = first or time.perf_counter()
                            result += token
            ended = time.perf_counter()
            assert result and first is not None and count > 0, "Incomplete stream/usage"
            print(json.dumps({
                "run": run + 1, "first_token_ms": round((first - started) * 1000, 1),
                "total_s": round(ended - started, 2), "output_tokens": count,
                "decode_tokens_per_s": round((count - 1) / (ended - first), 1),
            }))
        response = await client.post(
            f"{settings.ollama_host}/api/embeddings",
            json={"model": settings.embedding_model, "prompt": "Embedding smoke test"},
        )
        response.raise_for_status()
        assert len(response.json()["embedding"]) == 768
        print("CPU Nomic embeddings: 768 dimensions OK")


if __name__ == "__main__":
    asyncio.run(main())
