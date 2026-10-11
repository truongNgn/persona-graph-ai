# Local runtime guides

See the [project README](../../README.md) for the application overview.

- [llama.cpp operations](WSL_LLAMA_CPP.md): current WSL Docker architecture, model storage, GPU tuning, dev login, validation and optional vLLM rollback.
- [WSL migration history](WSL_VLLM_MIGRATION.md): the earlier Docker Desktop-to-WSL migration, backups and embedding preservation. Its vLLM deployment has been superseded by llama.cpp.

The current stack uses llama.cpp for chat, CPU-only Ollama for Nomic embeddings,
CPU Kokoro ONNX for speech, faster-whisper for optional microphone transcription,
and PostgreSQL for authentication and conversations. Runtime weights, credentials,
voice recordings and vector databases are excluded from Git.
