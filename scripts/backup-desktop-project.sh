#!/usr/bin/env bash
# Read-only exports from Desktop into Ubuntu's E:-backed filesystem.
set -euo pipefail
backup=/var/lib/persona-migration
install -d -m 0700 "$backup"
old=(docker --host unix:///var/run/docker.sock)
[[ $("${old[@]}" info --format '{{.Name}}') == docker-desktop ]] || { echo 'Source is not Docker Desktop'; exit 1; }
"${old[@]}" exec persona-graph-ai-postgres-1 pg_dump -U chatbot -d chatbot -Fc > "$backup/postgres.dump"
manifest=models/manifests/registry.ollama.ai/library/nomic-embed-text/latest
mapfile -t blobs < <("${old[@]}" exec persona-graph-ai-ollama-1 cat "/root/.ollama/$manifest" | python3 -c 'import json,sys; m=json.load(sys.stdin); print("\n".join("models/blobs/"+x["digest"].replace(":","-") for x in [m["config"],*m["layers"]]))')
"${old[@]}" exec persona-graph-ai-ollama-1 tar -C /root/.ollama -cf - "$manifest" "${blobs[@]}" > "$backup/nomic.tar"
for volume in chroma_data session_history; do
  "${old[@]}" run --rm -v "persona-graph-ai_${volume}:/source:ro" postgres:16-alpine tar -C /source -cf - . > "$backup/${volume}.tar"
done
chmod 0600 "$backup"/*
ls -lh "$backup"
