#!/usr/bin/env bash
# Restore the E:-backed backups once into the independent WSL daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
backup=/var/lib/persona-migration
new=(docker --host unix:///run/persona-docker.sock)
old=(docker --host unix:///var/run/docker.sock)
[[ -s "$backup/postgres.dump" && -s "$backup/nomic.tar" ]] || { echo 'Backups missing'; exit 1; }
[[ ! -e "$backup/restored" ]] || { echo 'Already restored; refusing to overwrite live data'; exit 1; }
for volume in ollama_data chroma_data session_history; do
  "${new[@]}" volume create "persona-graph-ai_${volume}" >/dev/null
done
"${new[@]}" run --rm -i -v persona-graph-ai_ollama_data:/target postgres:16-alpine tar -C /target -xf - < "$backup/nomic.tar"
for volume in chroma_data session_history; do
  "${new[@]}" run --rm -i -v "persona-graph-ai_${volume}:/target" postgres:16-alpine tar -C /target -xf - < "$backup/${volume}.tar"
done
"${old[@]}" stop persona-graph-ai-postgres-1
"${new[@]}" compose up -d --wait postgres
"${new[@]}" compose exec -T postgres pg_restore -U chatbot -d chatbot --exit-on-error < "$backup/postgres.dump"
"${new[@]}" compose exec -T postgres psql -U chatbot -d chatbot -c 'SELECT count(*) AS users FROM users; SELECT count(*) AS conversations FROM conversations; SELECT count(*) AS messages FROM messages;'
touch "$backup/restored"
