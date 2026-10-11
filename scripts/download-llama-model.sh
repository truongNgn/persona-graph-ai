#!/usr/bin/env bash
# Native WSL Docker only; weights remain in the E:-backed Ubuntu disk.
set -euo pipefail
docker_cmd=(docker --host unix:///run/persona-docker.sock)
volume=persona-graph-ai_llama_models
file=qwen2.5-1.5b-instruct-q4_k_m.gguf
revision=91cad51170dc346986eccefdc2dd33a9da36ead9
sha=6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e
"${docker_cmd[@]}" volume create "$volume" >/dev/null
path=$("${docker_cmd[@]}" volume inspect --format '{{.Mountpoint}}' "$volume")
case "$path" in /var/lib/persona-docker/volumes/*/_data) ;; *) echo 'Unexpected model volume path' >&2; exit 1 ;; esac
cd "$path"
if [[ -f "$file" ]] && echo "$sha  $file" | sha256sum --check --status; then
    echo "Verified existing model: $file"
    exit 0
fi
curl --fail --location --retry 5 --continue-at - --output "$file.part" \
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/$revision/$file"
echo "$sha  $file.part" | sha256sum --check
mv -- "$file.part" "$file"
echo "Installed verified model: $file"
