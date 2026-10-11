"""Serialized CPU voice cloning with a bounded reference-conditioning cache."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
lock = threading.Lock()
conditionals: OrderedDict[tuple[str, int, int], Any] = OrderedDict()
VOICE_ROOT = Path(os.environ.get("VOICE_ROOT", "/voices")).resolve()
MODEL_REVISION = "71ccd1d0081b430592cea481f4307e764e07bc64"


def load_model() -> Any:
    import torch
    from huggingface_hub import snapshot_download
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    torch.set_num_threads(int(os.environ.get("CHATTERBOX_CPU_THREADS", "4")))
    torch.set_num_interop_threads(1)
    directory = snapshot_download(
        "ResembleAI/chatterbox-nano", revision=MODEL_REVISION,
        allow_patterns=["ve.safetensors", "t3_nano_v1.safetensors",
                        "s3gen_meanflow.safetensors", "*.json", "merges.txt", "conds.pt"],
    )
    return ChatterboxTurboTTS.from_local(directory, device="cpu", nano=True)


async def initialize(app: FastAPI) -> None:
    try:
        app.state.model = await asyncio.to_thread(load_model)
        app.state.status = "ready"
    except Exception:
        logger.exception("Nano model startup failed")
        app.state.status = "failed"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.model = None
    app.state.status = "loading"
    task = asyncio.create_task(initialize(app))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Chatterbox-Nano CPU voice cloning", lifespan=lifespan)


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    voice: str = Field(min_length=1, max_length=240)


def reference_path(voice: str) -> Path:
    if "\\" in voice or voice.startswith("/") or ".." in voice.split("/"):
        raise HTTPException(400, "Invalid voice reference.")
    parts = voice.split("/")
    if len(parts) != 2 or any(not part for part in parts):
        raise HTTPException(400, "Invalid voice reference.")
    path = (VOICE_ROOT / voice).resolve()
    if not path.is_relative_to(VOICE_ROOT) or not path.is_file() or path.suffix != ".wav":
        raise HTTPException(404, "Voice recording not found.")
    return path


@app.post("/synthesize")
def synthesize(payload: SpeechRequest, request: Request) -> Response:
    if not payload.text.strip():
        raise HTTPException(422, "Text must contain words.")
    model = request.app.state.model
    if model is None:
        raise HTTPException(503, "Voice-cloning model is loading or unavailable.")
    path = reference_path(payload.voice)
    stat = path.stat()
    key = (payload.voice, stat.st_mtime_ns, stat.st_size)
    started = time.perf_counter()
    try:
        with lock:
            cached = conditionals.get(key)
            if cached is None:
                model.prepare_conditionals(str(path))
                conditionals[key] = model.conds
                while len(conditionals) > 8:
                    conditionals.popitem(last=False)
            else:
                model.conds = cached
            conditionals.move_to_end(key)
            audio = model.generate(payload.text.strip())
            samples = audio.squeeze().detach().cpu().numpy()
        if not np.isfinite(samples).all() or len(samples) == 0:
            raise RuntimeError("Invalid generated audio")
        buffer = io.BytesIO()
        sf.write(buffer, samples, model.sr, format="WAV", subtype="PCM_16")
        elapsed = time.perf_counter() - started
        logger.warning("Nano synthesis %.2fs; audio %.2fs; cached=%s",
                       elapsed, len(samples) / model.sr, cached is not None)
        return Response(buffer.getvalue(), media_type="audio/wav",
                        headers={"X-Synthesis-Seconds": f"{elapsed:.3f}"})
    except Exception as exc:
        logger.exception("Voice synthesis failed")
        raise HTTPException(500, "Voice cloning failed.") from exc


@app.get("/health")
def health(request: Request) -> Response:
    import json
    ready = request.app.state.model is not None
    return Response(json.dumps({"status": request.app.state.status, "model_loaded": ready,
                                 "model": "Chatterbox-Nano", "device": "cpu"}),
                    status_code=200 if ready else 503, media_type="application/json")
