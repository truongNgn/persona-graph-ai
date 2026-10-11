#!/usr/bin/env bash
# Copy existing Windows dev RAG/history into WSL volumes; keep both originals
# and a snapshot of the destination. Stop the Windows backend before running.
set -euo pipefail
cd "$(dirname "$0")/.."
backup=/var/lib/persona-migration
[[ ! -e "$backup/dev-data-imported" ]] || { echo 'Already imported; refusing to overwrite live data'; exit 1; }
docker_cmd=(docker --host unix:///run/persona-docker.sock)
"${docker_cmd[@]}" compose --profile app stop backend
for pair in 'chroma_data:backend/chroma_data' 'session_history:backend/data/session_history'; do
  volume=${pair%%:*}
  source=${pair#*:}
  if [[ -d "$source" ]]; then
    "${docker_cmd[@]}" run --rm -v "persona-graph-ai_${volume}:/source:ro" postgres:16-alpine tar -C /source -cf - . > "$backup/pre-dev-${volume}.tar"
    tar -C "$source" -cf - . | "${docker_cmd[@]}" run --rm -i -v "persona-graph-ai_${volume}:/target" postgres:16-alpine tar -C /target -xf -
  fi
done
touch "$backup/dev-data-imported"
"${docker_cmd[@]}" compose --profile app up -d --no-build backend
