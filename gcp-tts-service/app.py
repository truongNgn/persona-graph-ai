"""CPU-only Kokoro ONNX speech service, usable locally or on Cloud Run."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import onnxruntime as ort
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, Response
from kokoro_onnx import Kokoro
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)
inference_lock = threading.Lock()


def load_model() -> Kokoro:
    model_dir = Path(os.environ.get("KOKORO_MODEL_DIR", "/app/models"))
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(os.environ.get("KOKORO_CPU_THREADS", "4"))
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_dir / "kokoro-v1.0.int8.onnx"), sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    return Kokoro.from_session(session, str(model_dir / "voices-v1.0.bin"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.kokoro = await asyncio.to_thread(load_model)
    yield
    app.state.kokoro = None


app = FastAPI(title="Local Kokoro CPU TTS", lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = "af_bella"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    emotion: str = "neutral"  # Compatibility field; Kokoro does not control emotion.

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("Text must contain words.")
        return text.strip()


@app.post("/synthesize")
def synthesize(payload: TTSRequest, request: Request) -> Response:
    model: Kokoro | None = getattr(request.app.state, "kokoro", None)
    if model is None:
        raise HTTPException(status_code=503, detail="TTS model is not loaded.")
    voice = payload.voice or "af_bella"
    if voice == "default":
        voice = "af_bella"
    if voice not in model.get_voices():
        raise HTTPException(status_code=400, detail=f"Unknown Kokoro voice: {voice}")
    if voice[:1] not in {"a", "b"}:
        raise HTTPException(status_code=400, detail="Select an English Kokoro voice.")
    started = time.perf_counter()
    try:
        # Sync routes run in FastAPI's worker threads; serialize CPU inference.
        with inference_lock:
            samples, sample_rate = model.create(
                payload.text, voice=voice, speed=payload.speed,
                lang="en-gb" if voice.startswith("b") else "en-us",
            )
        buffer = io.BytesIO()
        sf.write(buffer, samples, sample_rate, format="WAV", subtype="PCM_16")
        logger.info("Kokoro voice=%s synthesis=%.2fs audio=%.2fs", voice,
                    time.perf_counter() - started, len(samples) / sample_rate)
        return Response(content=buffer.getvalue(), media_type="audio/wav")
    except Exception as exc:
        logger.exception("Kokoro synthesis failed")
        raise HTTPException(status_code=500, detail="Speech synthesis failed.") from exc


@app.get("/health")
def health(request: Request) -> dict:
    model: Kokoro | None = getattr(request.app.state, "kokoro", None)
    if model is None:
        raise HTTPException(status_code=503, detail="TTS model is not loaded.")
    return {"status": "healthy", "model_loaded": True, "model": "Kokoro-82M-v1.0-int8",
            "providers": model.sess.get_providers()}


@app.get("/voices")
def voices(request: Request) -> dict[str, list[str]]:
    model: Kokoro = request.app.state.kokoro
    return {"voices": sorted(voice for voice in model.get_voices() if voice[:1] in {"a", "b"})}
