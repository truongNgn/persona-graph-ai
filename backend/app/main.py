"""FastAPI app setup and route registration."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response

from pydantic import BaseModel, Field
from app.models import SentenceChunk
from app.voice_references import normalize_reference, owns_voice_reference

from app.auth import AuthContext, get_request_auth_context, issue_dev_token
from app.character_registry import character_registry
from app.config import settings
from app.conversation_store import (
    LoginRequest,
    RegisterRequest,
    authenticate_user,
    create_user,
    delete_conversation,
    get_conversation_detail,
    get_or_create_google_user,
    get_or_create_dev_user,
    get_user,
    list_conversations,
    user_public_dict,
)
from app.db import db_ready, init_db
from app.feedback import RatingRequest, default_feedback_store
from app.gateway.websocket import websocket_chat as gateway_websocket_chat
from app.llm_handler import get_llm_handler
from app.rate_limit import RateLimitMiddleware
from app.session_history import session_history_ready
from app.stt_handler import BaseSTTHandler, get_stt_handler, validate_audio_upload
from app.tts_handler import BaseTTSHandler, get_tts_handler
import httpx
from app.warmup import start_background_warmup, warmup_state
from app.model_registry import model_registry
from app.model_availability import is_model_available
from app.lc_chain import resolve_model_info

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
SUPPORTED_VOICE_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
SUPPORTED_AVATAR_SUFFIXES = {".glb"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup Guard Secrets Check (Stage 9) ──────────────────────────────────
    if settings.deployment_mode == "cloud":
        missing_keys = []
        if not settings.gemini_api_key:
            missing_keys.append("GEMINI_API_KEY")
        if not settings.deepseek_api_key:
            missing_keys.append("DEEPSEEK_API_KEY")
        if settings.auth_token_secret == "change-me-dev-secret":
            missing_keys.append("AUTH_TOKEN_SECRET (cannot be the default 'change-me-dev-secret')")
        if missing_keys:
            error_msg = f"Startup Guard: Missing required cloud secrets/keys: {', '.join(missing_keys)} in deployment_mode='cloud'."
            logger.critical(error_msg)
            raise ValueError(error_msg)
    else:
        missing_keys = []
        if not settings.gemini_api_key:
            missing_keys.append("GEMINI_API_KEY")
        if not settings.deepseek_api_key:
            missing_keys.append("DEEPSEEK_API_KEY")
        if missing_keys:
            logger.warning(
                "Startup Guard: Missing API keys in local mode: %s. Cloud models will not be functional.",
                ", ".join(missing_keys),
            )

    # ── LangSmith Observability status check ──────────────────────────────────
    import os
    tracing_enabled = os.environ.get("LANGCHAIN_TRACING_V2") or os.environ.get("LANGSMITH_TRACING")
    project = os.environ.get("LANGCHAIN_PROJECT") or os.environ.get("LANGSMITH_PROJECT")
    api_key = os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY")
    if tracing_enabled == "true":
        if api_key:
            logger.info("LangSmith Tracing is ENABLED. Project: '%s'. Traces will be sent to LangSmith.", project)
        else:
            logger.warning("LangSmith Tracing is set to true, but API KEY is missing! Traces will not be sent.")
    else:
        logger.info("LangSmith Tracing is DISABLED.")

    # ── TTS handler setup ─────────────────────────────────────────────────────
    # Heavy LLM/XTTS warm-up runs in the background by default so FastAPI can
    # accept WebSocket connections immediately after the process starts.
    tts = get_tts_handler()
    if tts.is_active:
        if tts.provider_name.startswith("elevenlabs"):
            logger.info("TTS: %s ready (voice=%s).", tts.provider_name, settings.elevenlabs_voice_id)
        elif tts.provider_name == "xtts":
            logger.info("TTS: Coqui XTTS-v2 configured; background warmup will preload it.")
        else:
            logger.info("TTS: %s ready.", tts.provider_name)
    else:
        logger.warning("TTS: Running in text-only mode.")

    stt = get_stt_handler()
    if stt.is_active:
        logger.info("STT: %s ready.", settings.stt_provider)
    else:
        logger.info("STT: disabled.")

    app.state.tts_handler = tts
    app.state.stt_handler = stt
    app.state.db_ready = False
    if settings.database_auto_create:
        app.state.db_ready = await init_db()
    warmup_task = start_background_warmup(tts, stt)
    if warmup_task and settings.warmup_blocking:
        logger.info("Warmup: blocking startup until warmup completes...")
        await warmup_task
    try:
        yield
    finally:
        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
            try:
                await warmup_task
            except asyncio.CancelledError:
                logger.info("Warmup: cancelled during shutdown.")


app = FastAPI(
    title="Interactive Chatbot — AI Core + TTS",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

# Local-dev-only static serving for per-user custom avatars. Unlike voice
# uploads (consumed server-side by the TTS pipeline, never fetched by the
# browser), custom 3D models must be reachable by the browser's GLTFLoader
# directly. When GCS_BUCKET_NAME is set (cloud deploy), this mount is unused
# — avatars are served from GCS instead (see get_character_avatar()).
_LOCAL_AVATARS_DIR = BACKEND_ROOT / "avatars"
_LOCAL_AVATARS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/avatars", StaticFiles(directory=_LOCAL_AVATARS_DIR), name="custom_avatars")


async def _llm_health() -> bool:
    """Probe the configured LLM provider without letting a misconfigured
    provider turn the health/ready probes into an opaque 500."""
    try:
        return await get_llm_handler().health_check()
    except Exception as exc:
        logger.error("LLM provider '%s' unavailable: %s", settings.llm_provider, exc)
        return False


@app.get("/health")
async def health():
    llm_ok = await _llm_health()
    tts: BaseTTSHandler = app.state.tts_handler
    return {
        "status": "ok" if llm_ok else "degraded",
        "llm": {"provider": settings.llm_provider, "ready": llm_ok},
        "warmup": warmup_state.to_dict(),
        "tts": {
            "provider": tts.provider_name,
            "ready": tts.is_active,
        },
        "stt": {
            "provider": settings.stt_provider if settings.stt_enabled else "none",
            "ready": app.state.stt_handler.is_active,
            "language": settings.stt_language or None,
        },
    }


@app.get("/ready")
async def readiness():
    llm_ok = await _llm_health()
    history_ok = session_history_ready()
    database_ok = await db_ready()
    tts: BaseTTSHandler = app.state.tts_handler
    ready = llm_ok and history_ok
    return {
        "status": "ready" if ready else "degraded",
        "app": {"ready": True},
        "llm": {"provider": settings.llm_provider, "ready": llm_ok},
        "session_history": {
            "backend": settings.session_backend,
            "ready": history_ok,
        },
        "vector_store": {
            "provider": "chroma",
            "ready": True,
            "path": settings.chroma_path,
        },
        "database": {
            "provider": "postgres",
            "configured": bool(settings.database_url),
            "ready": database_ok,
        },
        "tts": {"provider": tts.provider_name, "ready": await tts.health_check() if hasattr(tts, "health_check") else tts.is_active},
        "warmup": warmup_state.to_dict(),
    }


def dev_quick_login_available() -> bool:
    return settings.deployment_mode == "local" and settings.dev_quick_login_enabled


@app.get("/api/auth/dev-status")
async def dev_login_status() -> dict[str, bool]:
    return {"enabled": dev_quick_login_available()}


@app.post("/api/auth/dev-login")
async def dev_login():
    if not dev_quick_login_available():
        raise HTTPException(status_code=404, detail="Development login is disabled.")
    try:
        user = await get_or_create_dev_user()
    except Exception as exc:
        logger.error("Development login account unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Development account is unavailable. Please retry.") from exc
    return {"access_token": issue_dev_token(user.id), "token_type": "bearer", "user": user_public_dict(user)}


@app.post("/api/auth/register")
async def register(payload: RegisterRequest):
    try:
        user = await create_user(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    token = issue_dev_token(user.id)
    return {"access_token": token, "token_type": "bearer", "user": user_public_dict(user)}


@app.post("/api/auth/login")
async def login(payload: LoginRequest):
    user = await authenticate_user(payload)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = issue_dev_token(user.id)
    return {"access_token": token, "token_type": "bearer", "user": user_public_dict(user)}


class GoogleLoginRequest(BaseModel):
    credential: str


@app.post("/api/auth/google")
async def login_google(payload: GoogleLoginRequest):
    if not settings.google_client_id:
        raise HTTPException(
            status_code=500,
            detail="Google Client ID is not configured on the backend. Please configure GOOGLE_CLIENT_ID."
        )

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests

        id_info = id_token.verify_oauth2_token(
            payload.credential,
            requests.Request(),
            settings.google_client_id
        )

        if id_info.get("iss") not in ["accounts.google.com", "https://accounts.google.com"]:
            raise ValueError("Invalid issuer.")

        email = id_info.get("email")
        if not email:
            raise ValueError("Email not present in Google token.")

        display_name = id_info.get("name")
    except Exception as exc:
        logger.error("Google login failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail=f"Invalid Google token: {exc}"
        ) from exc

    user = await get_or_create_google_user(email, display_name)
    token = issue_dev_token(user.id)
    return {"access_token": token, "token_type": "bearer", "user": user_public_dict(user)}


@app.get("/api/auth/me")
async def me(auth: AuthContext = Depends(get_request_auth_context)):
    user = await get_user(auth.user_id)
    if user:
        return {"user": user_public_dict(user), "auth": {"mode": auth.mode}}
    raise HTTPException(status_code=404, detail="Authenticated user not found.")


@app.get("/api/conversations")
async def conversations(auth: AuthContext = Depends(get_request_auth_context)):
    return {"conversations": [item.model_dump() for item in await list_conversations(auth.user_id)]}


@app.get("/api/conversations/{conversation_id}")
async def conversation_detail(
    conversation_id: str,
    auth: AuthContext = Depends(get_request_auth_context),
):
    detail = await get_conversation_detail(auth.user_id, conversation_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return detail.model_dump()


@app.delete("/api/conversations/{conversation_id}")
async def remove_conversation(
    conversation_id: str,
    auth: AuthContext = Depends(get_request_auth_context),
):
    deleted = await delete_conversation(auth.user_id, conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"ok": True}


@app.get("/api/voices")
async def get_voices(auth: AuthContext = Depends(get_request_auth_context)):
    voices = []
    if settings.tts_provider in {"kokoro", "chatterbox-nano"}:
        presets = ["af_bella", "am_michael"]
        clone_ready = False
        clone_status = "unavailable"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(settings.kokoro_tts_url.rsplit("/", 1)[0] + "/voices")
                response.raise_for_status()
                presets = response.json()["voices"]
        except Exception as exc:
            logger.warning("Kokoro voice discovery failed: %s", exc)
        cloning = settings.tts_provider == "chatterbox-nano"
        if cloning:
            voices_dir = BACKEND_ROOT / "voices" / auth.user_id
            if voices_dir.is_dir():
                voices = [f"{auth.user_id}/{p.name}" for p in voices_dir.glob("*.wav") if p.is_file()]
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    status = await client.get(settings.chatterbox_tts_url.rsplit("/", 1)[0] + "/health")
                    clone_ready = status.status_code == 200 and status.json().get("model_loaded") is True
                    clone_status = status.json().get("status", "unavailable")
            except Exception:
                pass
        return {"voices": sorted(voices) + presets, "provider": settings.tts_provider,
                "supports_voice_cloning": cloning, "clone_ready": clone_ready, "clone_status": clone_status}

    if settings.gcs_bucket_name:
        try:
            from google.cloud import storage
            storage_client = storage.Client()
            bucket = storage_client.bucket(settings.gcs_bucket_name)
            prefix = f"voices/{auth.user_id}/"
            blobs = bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                name = blob.name.replace("voices/", "")
                if name and Path(name).suffix.lower() in SUPPORTED_VOICE_SUFFIXES:
                    voices.append(name)
        except Exception as exc:
            logger.error("Failed to list GCS voices: %s", exc)

    voices_dir = BACKEND_ROOT / "voices" / auth.user_id
    if voices_dir.exists():
        local_voices = [
            f"{auth.user_id}/{f.name}" for f in voices_dir.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_VOICE_SUFFIXES
        ]
        for name in local_voices:
            if name not in voices:
                voices.append(name)

    return {"voices": sorted(voices)}


class VoicePreviewRequest(BaseModel):
    voice: str = Field(min_length=1, max_length=240)
    text: str = Field(default="Hello. This is a preview of my voice.", min_length=1, max_length=300)


@app.post("/api/voices/preview")
async def preview_voice(payload: VoicePreviewRequest, auth: AuthContext = Depends(get_request_auth_context)):
    if settings.tts_provider not in {"kokoro", "chatterbox-nano"}:
        raise HTTPException(400, "Voice preview is unavailable for this provider.")
    reference = "/" in payload.voice or "\\" in payload.voice
    if reference:
        if settings.tts_provider != "chatterbox-nano" or not owns_voice_reference(payload.voice, auth.user_id):
            raise HTTPException(403, "You can only preview your own voice recordings.")
        if not (BACKEND_ROOT / "voices" / payload.voice).is_file():
            raise HTTPException(404, "Voice recording not found.")
    url = settings.chatterbox_tts_url if reference else settings.kokoro_tts_url
    try:
        async with httpx.AsyncClient(timeout=settings.chatterbox_tts_timeout_seconds) as client:
            result = await client.post(url, json={"voice": payload.voice, "text": payload.text})
            result.raise_for_status()
    except httpx.HTTPError as exc:
        # Preview must never silently substitute a different speaker.
        raise HTTPException(503, "Voice preview failed. The cloning model may still be loading.") from exc
    return Response(result.content, media_type="audio/wav")


@app.post("/api/voices/upload")
async def upload_voice(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_request_auth_context)
):
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if settings.tts_provider == "chatterbox-nano":
        if ext not in SUPPORTED_VOICE_SUFFIXES | {".webm", ".mp4"}:
            raise HTTPException(400, "Upload a WAV, MP3, or browser audio recording.")
        content = await file.read(settings.voice_max_file_mb * 1024 * 1024 + 1)
        if len(content) > settings.voice_max_file_mb * 1024 * 1024:
            raise HTTPException(413, f"Voice recording exceeds {settings.voice_max_file_mb} MB.")
        try:
            normalized, duration = await asyncio.to_thread(normalize_reference, content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        stem = "".join(c for c in Path(file.filename or "recording").stem if c.isalnum() or c in "-_")[:40] or "recording"
        filename = f"{stem}-{uuid4().hex[:12]}.wav"
        voices_dir = BACKEND_ROOT / "voices" / auth.user_id
        voices_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((voices_dir / filename).write_bytes, normalized)
        return {"filename": f"{auth.user_id}/{filename}", "duration_seconds": duration}
    if ext not in SUPPORTED_VOICE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format. Supported: {SUPPORTED_VOICE_SUFFIXES}",
        )

    filename = Path(file.filename).name if file.filename else "voice.wav"
    user_voice_key = f"{auth.user_id}/{filename}"

    if settings.gcs_bucket_name:
        try:
            from google.cloud import storage
            storage_client = storage.Client()
            bucket = storage_client.bucket(settings.gcs_bucket_name)
            blob = bucket.blob(f"voices/{user_voice_key}")

            content = await file.read()
            blob.upload_from_string(content, content_type=file.content_type)
            logger.info("Voice uploaded to GCS for user %s: %s", auth.user_id, filename)
        except Exception as exc:
            logger.error("Failed to upload voice to GCS: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload voice to GCS: {exc}",
            ) from exc
    else:
        voices_dir = BACKEND_ROOT / "voices" / auth.user_id
        voices_dir.mkdir(parents=True, exist_ok=True)
        target_path = voices_dir / filename
        try:
            content = await file.read()
            with open(target_path, "wb") as f:
                f.write(content)
            logger.info("Voice uploaded locally for user %s: %s", auth.user_id, target_path)
        except Exception as exc:
            logger.error("Failed to save uploaded voice locally: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save voice file locally: {exc}",
            ) from exc

    return {"filename": user_voice_key}


@app.get("/api/characters")
async def get_characters():
    return {"characters": character_registry.list_public(), "default": settings.default_character_id}


@app.get("/api/characters/{character_id}/avatar")
async def get_character_avatar(
    character_id: str,
    request: Request,
    auth: AuthContext = Depends(get_request_auth_context),
):
    """Resolve the avatar to use for this character for the current user:
    their own uploaded override if one exists (see POST below), else the
    character's default from the registry. Auth-gated (unlike GET
    /api/characters, which is public) because the answer depends on the
    caller's identity — see docs/RAG_character_roleplay_implementation_plan.md
    §2.3."""
    if settings.gcs_bucket_name:
        try:
            from google.cloud import storage
            storage_client = storage.Client()
            bucket = storage_client.bucket(settings.gcs_bucket_name)
            blob = bucket.blob(f"avatars/custom/{auth.user_id}/{character_id}.glb")
            if blob.exists():
                return {"avatar": f"https://storage.googleapis.com/{settings.gcs_bucket_name}/avatars/custom/{auth.user_id}/{character_id}.glb"}
        except Exception as exc:
            logger.error("Failed to check GCS custom avatar for %s: %s", character_id, exc)
    else:
        local_path = _LOCAL_AVATARS_DIR / auth.user_id / f"{character_id}.glb"
        if local_path.exists():
            base = str(request.base_url).rstrip("/")
            return {"avatar": f"{base}/static/avatars/{auth.user_id}/{character_id}.glb"}

    character = character_registry.get(character_id)
    return {"avatar": character.get("avatar") if character else None}


@app.post("/api/characters/{character_id}/avatar")
async def upload_character_avatar(
    character_id: str,
    request: Request,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_request_auth_context),
):
    """Persist a per-user custom 3D model for one character. Mirrors
    /api/voices/upload's GCS-vs-local branching. Keyed by
    (user_id, character_id) — never touches character_registry / the
    character's lore, so the character's "brain" stays the same regardless
    of which avatar is displayed (see plan §2.3)."""
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in SUPPORTED_AVATAR_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported 3D model format. Supported: {SUPPORTED_AVATAR_SUFFIXES}",
        )

    if not character_registry.get(character_id):
        raise HTTPException(status_code=404, detail=f"Unknown character_id: {character_id}")

    content = await file.read()

    if settings.gcs_bucket_name:
        try:
            from google.cloud import storage
            storage_client = storage.Client()
            bucket = storage_client.bucket(settings.gcs_bucket_name)
            blob = bucket.blob(f"avatars/custom/{auth.user_id}/{character_id}.glb")
            blob.upload_from_string(content, content_type=file.content_type)
            logger.info("Custom avatar uploaded to GCS: user=%s character=%s", auth.user_id, character_id)
            return {"avatar": f"https://storage.googleapis.com/{settings.gcs_bucket_name}/avatars/custom/{auth.user_id}/{character_id}.glb"}
        except Exception as exc:
            logger.error("Failed to upload custom avatar to GCS: %s", exc)
            raise HTTPException(status_code=500, detail=f"Failed to upload avatar to GCS: {exc}") from exc

    avatars_dir = _LOCAL_AVATARS_DIR / auth.user_id
    avatars_dir.mkdir(parents=True, exist_ok=True)
    target_path = avatars_dir / f"{character_id}.glb"
    try:
        with open(target_path, "wb") as f:
            f.write(content)
        logger.info("Custom avatar saved locally: user=%s character=%s path=%s", auth.user_id, character_id, target_path)
    except Exception as exc:
        logger.error("Failed to save custom avatar locally: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to save avatar locally: {exc}") from exc

    base = str(request.base_url).rstrip("/")
    return {"avatar": f"{base}/static/avatars/{auth.user_id}/{character_id}.glb"}


@app.get("/api/avatars")
async def get_avatars():
    models_dir = PROJECT_ROOT / "frontend" / "public" / "models"
    if not models_dir.exists():
        return {"avatars": []}
    avatars = sorted(
        f.name for f in models_dir.iterdir()
        if f.is_file() and f.suffix.lower() == ".glb"
    )
    return {"avatars": avatars}


@app.get("/api/llm/models")
async def get_llm_models():
    # 1. Discover local models (populates registry with local Ollama models if in local mode)
    if settings.deployment_mode == "local":
        await model_registry.discover_local_models()

    # Get candidate models
    candidates = model_registry.list_all()

    # Filter and check reachability concurrently
    tasks = []
    filtered_candidates = []
    for info in candidates:
        # Condition 1: deployment mode check
        if settings.deployment_mode == "cloud" and info.deployment == "local":
            continue

        # Condition 2: requires keys check
        has_requires = True
        for req in info.requires:
            if not getattr(settings, req, None):
                has_requires = False
                break
        if not has_requires:
            continue

        filtered_candidates.append(info)
        tasks.append(is_model_available(info))

    reachability_results = await asyncio.gather(*tasks)

    # Group the models
    groups_dict = {
        "cloud-api": {"label": "Cloud API", "models": []},
        "self-hosted": {"label": "Self-hosted", "models": []},
        "local": {"label": "Local (Ollama)", "models": []},
    }

    reachable_models = [info for info, reachable in zip(filtered_candidates, reachability_results) if reachable]

    # Deduplicate local models: if a model was dynamically discovered (id starts with "ollama:"),
    # hide the static catalog models ("ollama-large", "ollama-small") with the same underlying model name.
    discovered_model_names = {
        info.model.lower().strip() for info in reachable_models
        if info.id.startswith("ollama:")
    }

    final_models = []
    for info in reachable_models:
        if info.id in ("ollama-large", "ollama-small") and info.model.lower().strip() in discovered_model_names:
            continue
        final_models.append(info)

    seen_self_hosted: set[str] = set()
    for info in final_models:
        if info.deployment == "self-hosted":
            if info.model in seen_self_hosted:
                continue
            seen_self_hosted.add(info.model)
        dep = info.deployment
        if dep in groups_dict:
            groups_dict[dep]["models"].append({
                "id": info.id,
                "display_name": info.display_name,
                "tier": info.tier,
            })

    # Remove empty groups
    groups = [g for g in groups_dict.values() if g["models"]]

    # Get default model ID
    try:
        default_model = resolve_model_info(None, None).id
    except Exception:
        default_model = "local-large"

    # If the default model ID is one of the skipped static models, map it to the discovered model ID
    if default_model in ("ollama-large", "ollama-small"):
        static_info = model_registry.get(default_model)
        if static_info:
            target_model_name = static_info.model.lower().strip()
            for info in final_models:
                if info.id.startswith("ollama:") and info.model.lower().strip() == target_model_name:
                    default_model = info.id
                    break

    return {
        "default": default_model,
        "groups": groups,
    }


@app.get("/api/stt/status")
async def get_stt_status():
    stt: BaseSTTHandler = app.state.stt_handler
    return {
        "enabled": stt.is_active,
        "provider": settings.stt_provider if stt.is_active else "none",
        "language": settings.stt_language or None,
        "max_file_mb": settings.stt_max_file_mb,
        "max_duration_seconds": settings.stt_max_duration_seconds,
        "message": getattr(stt, "reason", None),
    }


@app.post("/api/stt")
async def transcribe_audio(
    audio: UploadFile = File(...),
    mime_type: str = Form(""),
):
    stt: BaseSTTHandler = app.state.stt_handler
    if not stt.is_active:
        raise HTTPException(status_code=503, detail=getattr(stt, "reason", "STT is not enabled."))

    resolved_mime_type = mime_type or audio.content_type or ""
    audio_bytes = await audio.read(settings.stt_max_file_mb * 1024 * 1024 + 1)
    try:
        validate_audio_upload(audio_bytes, resolved_mime_type)
        result = await stt.transcribe(audio_bytes, resolved_mime_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("STT transcription failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result.model_dump()


@app.post("/api/feedback/rating")
async def submit_feedback_rating(
    rating: RatingRequest,
    auth: AuthContext = Depends(get_request_auth_context),
):
    await default_feedback_store.record_rating(rating.model_copy(update={"user_id": auth.user_id}))
    return {"ok": True}


@app.get("/api/feedback/session/{session_id}")
async def get_feedback_session(
    session_id: str,
    auth: AuthContext = Depends(get_request_auth_context),
):
    events = await default_feedback_store.get_session_events(session_id)
    return {"events": [event for event in events if event.get("user_id") == auth.user_id]}


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await gateway_websocket_chat(websocket)
