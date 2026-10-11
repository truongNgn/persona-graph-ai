# Local llama.cpp on the RTX 3050 laptop

See [project README](../../README.md) and [guide index](README.md).

The default runtime is the official CUDA llama.cpp server, pinned by image digest,
with Qwen2.5-1.5B-Instruct Q4_K_M GGUF. Its API alias remains `persona-qwen`.
Both routing tiers use one resident model. Native Docker Engine runs in Ubuntu
on `/run/persona-docker.sock`; the Ubuntu virtual disk resides under `E:\WSL\Ubuntu`.
No Docker Desktop integration is required.

## Start

From the repository in PowerShell. The WSL path below is an example for this
checkout; adjust it for your own clone. Install the native WSL Docker Engine
using `scripts/setup-wsl-docker.sh` first (see the project README).

For a new checkout, copy `backend/.env.example` to `backend/.env` once and set
`AUTH_TOKEN_SECRET` to a private value. Preserve an existing `.env` when upgrading.
The embedding service must have `nomic-embed-text` installed before chatting.

```powershell
wsl -d Ubuntu -u root -- bash /mnt/d/Coder-IT/AI/persona-graph-ai/scripts/download-llama-model.sh
./scripts/wsl-compose.ps1 up -d ollama postgres
./scripts/wsl-compose.ps1 exec ollama ollama pull nomic-embed-text
./scripts/wsl-compose.ps1 --profile app up -d --build
./scripts/wsl-compose.ps1 logs --tail 80 llama-cpp
```

The download helper uses a pinned Hugging Face revision, verifies SHA-256,
resumes interrupted downloads, and atomically installs the verified file.
The 1.12 GB model is in Docker volume `persona-graph-ai_llama_models`, mounted
read-only at `/models`. Native Docker stores that volume under
`/var/lib/persona-docker/volumes`, inside the E:-backed WSL disk.

Endpoints: frontend `http://localhost:8088`, backend `http://localhost:8001`,
inference `http://localhost:8080/v1`, CPU embeddings `http://localhost:11435`.
The existing dev quick-login button remains available when locally enabled.
CPU Kokoro speech, PostgreSQL, Chroma, history and Nomic embeddings are preserved.

`backend/.env` uses `LLM_PROVIDER=llama_cpp`, `LOCAL_LLM_BASE_URL`,
`LOCAL_LLM_LARGE_MODEL`, `LOCAL_LLM_SMALL_MODEL`, and `LOCAL_LLM_MAX_TOKENS`.
Legacy `VLLM_*` environment names are accepted when the corresponding new name
is absent. Saved `vllm`, `vllm-large`, and `vllm-small` selections map to the
current local inference endpoint; they do not start a separate vLLM process.

## Development login and microphone input

Set `DEV_QUICK_LOGIN_ENABLED=true` in the private `backend/.env`, then recreate
the backend. The login screen offers **Quick login as Dev Tester**. The account
is persisted and reused; no password is embedded in the frontend. This feature
is disabled by default and always disabled with `DEPLOYMENT_MODE=cloud`.

For microphone transcription, use `STT_ENABLED=true`, `STT_PROVIDER=faster-whisper`,
`STT_DEVICE=cpu` and `STT_COMPUTE_TYPE=int8`. The UI lets you choose an input
microphone and shows its audio level while recording. Its selected device is
stored locally. Grant microphone access in the browser; localhost is supported.

Kokoro serves English presets on CPU and does not clone uploaded voice samples
or control emotional delivery. Speech failures fall back to text. See the
project README for the model/voice configuration.

## Resource settings

One sequence, 4096-token context, four CPU threads, 256-token batch and
128-token microbatch keep resource use bounded. The small model initially
offloads all layers. GPU layers are configurable without changing code:

```powershell
$env:LLAMA_GPU_LAYERS='20'
./scripts/wsl-compose.ps1 up -d --force-recreate llama-cpp
```

Use fewer layers if other GPU workloads leave insufficient VRAM. `0` selects
CPU layers. Evaluate settings with the avatar open; a successful idle health
check does not establish smooth rendering under load. The server intentionally
does not auto-restart after WSL starts, so GPU initialization failures cannot
create a restart loop. Start it using Compose.

Run the non-persisting inference/embedding smoke test from `backend`:

```powershell
./.venv/Scripts/python.exe scripts/smoke_local_llm.py
```

It measures short already-loaded requests, not cold startup, full RAG/TTS
latency, concurrent load, or avatar frame stability. Compare those separately.

## Optional vLLM comparison / rollback

`docker-compose.vllm.yml` retains the prior constrained AWQ serving configuration.
Never run both chat engines together on the shared 4 GB GPU. Stop llama.cpp,
then explicitly start the optional service:

```powershell
./scripts/wsl-compose.ps1 stop llama-cpp
./scripts/wsl-compose.ps1 -f docker-compose.vllm.yml up -d vllm
```

For a Windows backend, set `LLM_PROVIDER=vllm`; its endpoint stays
`http://localhost:8080/v1`. For a container backend, override
`LOCAL_LLM_BASE_URL=http://vllm:8000/v1` in an additional Compose override.
The old image/model can be downloaded again if removed after validation.
Return by stopping vLLM and starting the default Compose stack.

Do not use `down -v` or global Docker prune: persistent databases and unrelated
workloads must be preserved. Deleting project inference artifacts frees space
inside WSL; Windows E: recovery may also need a later WSL VHDX compaction.

Sources: [llama.cpp Docker images](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md),
[server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
[official Qwen GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF).

## Validation on this laptop (2026-10-11)

- 63 backend tests passed, plus backend/frontend container builds (TypeScript/Vite).
- Dev quick login through the frontend proxy, new and legacy WebSocket selections,
  text generation, CPU Kokoro audio and PostgreSQL message persistence passed.
- Two 64-token already-loaded direct streams: 85.0 / 89.9 decode tokens/s;
  first-token latency 568.9 / 287.1 ms. Earlier vLLM short-stream reference:
  43.8 / 42.3 tokens/s and 574.5 / 49.2 ms. These are small smoke measurements,
  not a controlled sustained benchmark; first-token performance is not uniformly improved.
- Full application greeting checks: first text chunk 1151 ms; first audio chunk
  1122 ms. They include retrieval and, for voice, speech synthesis.
- llama.cpp container RAM approximately 1.16 GiB versus earlier vLLM 2.65 GiB.
  Total GPU usage across all applications approximately 3.0 GiB, leaving about
  0.9 GiB free at the final snapshot. Avatar frame rate was not benchmarked.
- Removed the retired vLLM container, image and its isolated AWQ model cache
  only after validation. The optional Compose configuration can re-download them.
- Fixed an existing undefined timestamp function that prevented PostgreSQL
  message writes, and set the container lore directory to its mounted path.

- Cleanup reduced Ubuntu filesystem usage from approximately 78 GiB to 39 GiB. Windows E: still has about 39 GB free because the virtual disk retains its expanded allocation; VHDX compaction was not performed while other Ubuntu workloads are running.
