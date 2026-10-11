# WSL Docker and vLLM migration

Historical migration record (2026-10-11). The current deployment uses
[llama.cpp](WSL_LLAMA_CPP.md); the vLLM container, image and AWQ cache were
retired after validating the replacement. This page records the earlier
migration and data-preservation checks. See [project README](../../README.md).

## Architecture and storage

Windows frontend/backend development processes call the Ubuntu WSL2 services:

```text
React -> FastAPI/LangGraph -> ChatOpenAI -> vLLM :8080/v1 -> Qwen AWQ
                         -> OllamaEmbeddings -> CPU Ollama :11435 -> Nomic
                         -> PostgreSQL :5433
```

Ubuntu's virtual disk is `E:\WSL\Ubuntu\ext4.vhdx`. The independent Docker
daemon uses `/var/lib/persona-docker` inside that disk. Its systemd unit is
`persona-docker.service`, its config `/etc/docker/persona-daemon.json`, and its
socket `/run/persona-docker.sock`. No Docker daemon TCP port is exposed.

The Docker Desktop daemon and unrelated containers are separate. Windows
`docker` may continue to target Desktop; always use `scripts/wsl-compose.ps1`
for this deployment. From Ubuntu use:

```bash
sudo docker --host unix:///run/persona-docker.sock compose up -d
```

Named volumes:

| Volume | Contents |
| --- | --- |
| `persona-graph-ai_huggingface_cache` | Qwen model weights/tokenizer and HF cache |
| `persona-graph-ai_ollama_data` | Nomic embedding weights only |
| `persona-graph-ai_postgres_data` | Authentication and conversation database |
| `persona-graph-ai_chroma_data` | Containerized backend vector database |
| `persona-graph-ai_session_history` | Containerized backend history |

The Windows development backend's existing `backend/chroma_data` and
`backend/data/session_history` stay in place as originals. After restoring the
Desktop volumes, `scripts/import-dev-data.sh` snapshots the destination and
copies Windows development data into the WSL volumes once. Both source and
destination contained 146 embeddings when verified. The Postgres migration
preserved four user accounts.

## Model and GPU limits

The measured GPU is an RTX 3050 Laptop with 4 GB VRAM shared with desktop apps.
Use one `Qwen/Qwen2.5-1.5B-Instruct-AWQ` checkpoint, exposed as `persona-qwen`.
Both routing tiers use this same served model. The second catalog entry is a
routing alias; it is hidden from the picker when it resolves to the same model.
This is a smaller model than the former Llama 3.2 3B and may reduce answer quality.

vLLM is pinned to `v0.11.0`. The deployment uses AWQ Marlin, float16, eager
execution, a 4096-token total context window, one concurrent sequence, chunked
prefill with 256 batched tokens, and an explicit 128 MiB KV cache. Responses are
capped at 512 tokens in the backend. History, character context, user input, and
output must fit within the total context limit.

The explicit KV cache setting overrides vLLM's automatic cache sizing from
`gpu_memory_utilization`. It does not cap total GPU memory: weights, activations,
CUDA runtime, and the KV cache all need free VRAM. If startup reports OOM, inspect
`nvidia-smi`, close GPU-heavy applications, and retry the service. The service
does not loop indefinitely on startup failures. Enable automatic restart only
after confirming sufficient free VRAM for your usual desktop workload.

Ollama receives no GPU devices and serves embeddings on CPU, preserving the
`ollama:nomic-embed-text` embedding signature and existing Chroma data. Changing
to a different embedding model requires a separate re-embedding migration.

## Historical setup and checks

Stop llama.cpp before testing the optional vLLM service. The default Compose
file now starts llama.cpp; use the explicit comparison file for vLLM.

```powershell
wsl -d Ubuntu -u root -- bash /mnt/d/Coder-IT/AI/persona-graph-ai/scripts/setup-wsl-docker.sh
./scripts/wsl-compose.ps1 -f docker-compose.vllm.yml up -d vllm
./scripts/wsl-compose.ps1 exec ollama ollama pull nomic-embed-text
./scripts/wsl-compose.ps1 ps
./scripts/wsl-compose.ps1 -f docker-compose.vllm.yml logs --tail 100 vllm
Invoke-RestMethod http://localhost:8080/v1/models
```

The setup script installs native Docker CE, Compose, and the NVIDIA Container
Toolkit using their official repositories. It prevents package installation
from starting Docker on Desktop's shared socket. The dedicated systemd unit
starts automatically when Ubuntu boots. GPU passthrough uses the Windows NVIDIA
driver; do not install a Linux display driver inside WSL.

The PowerShell wrapper stages a minimal backend build context on the Linux
filesystem. This avoids Windows ACL errors on old test temp directories and
does not upload virtualenvs or private runtime data to BuildKit. Source files
remain in the existing repository on D:.

Start the existing Windows backend with `backend/.venv/Scripts/python.exe
backend/run.py` from the repo root. For full container deployment, stop any
backend already using port 8001, then:

```powershell
./scripts/wsl-compose.ps1 --profile app up -d --build
```

Backend: `http://localhost:8001`; frontend: `http://localhost:8088`.
The app profile is optional so inference services can run independently while
the existing Windows development environment remains usable.

## Backup and cleanup

`scripts/backup-desktop-project.sh` exports a consistent PostgreSQL dump,
Nomic weights, and container Chroma/history archives to the private directory
`/var/lib/persona-migration` on E:. `scripts/restore-wsl-project.sh` imports them
once and refuses to overwrite an already restored deployment. Desktop source
data remains until the replacement has been checked.

After verifying streaming generation, embeddings, and database connectivity,
remove only this project's Desktop Ollama models/container/volume. Do not run
global Docker prune or delete unrelated project volumes. Other models in the
native Windows Ollama store may belong to unrelated work; they are not part of
this deployment.

Removing Docker Desktop model blobs frees space inside Desktop's virtual disk.
It may not immediately increase Windows C: free space because its VHDX may need
offline compaction. That is separate from this migration and requires scheduling
downtime for other Desktop workloads. New vLLM downloads and image layers are
stored on E: regardless.

The user's `.wslconfig` now points future WSL swap allocation to
`E:\WSL\swap.vhdx`. This applies after the next WSL restart; no global WSL
shutdown was performed because unrelated Desktop containers are running.
Windows' system-managed `C:\pagefile.sys` is separate from WSL swap and model
storage (approximately 25 GB allocated during this migration).

## Verified result (2026-10-11)

- vLLM GPU startup succeeded with a 4672-token KV cache and the 4096-token
  request context limit.
- LangChain streaming and a full frontend-proxied WebSocket -> LangGraph -> RAG
  -> vLLM chat succeeded with TTS disabled.
- Backend `/ready` reports vLLM, Postgres, and Chroma ready; frontend responds
  at `http://localhost:8088` and its model picker lists one vLLM option.
- CPU Nomic vectors have 768 dimensions and cosine similarity above 0.9999997
  against the former service.
- All 37 backend tests pass. Backend and frontend container builds pass.
- The old Desktop Llama/Qwen models, Ollama container, model volume, and image
  were removed. Native Windows Ollama models belonging to other work were kept.
- Backups remain at `/var/lib/persona-migration` in Ubuntu on E:. The stopped
  Desktop Postgres container/volume is retained as an additional rollback copy.

## References

- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [CUDA on WSL](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)
- [vLLM Docker deployment](https://docs.vllm.ai/en/v0.11.0/deployment/docker.html)
- [vLLM engine arguments](https://docs.vllm.ai/en/v0.11.0/configuration/engine_args.html)
- [Qwen AWQ model](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-AWQ)
