#!/usr/bin/env bash
# Avoid traversing Windows virtualenvs/test temp directories during BuildKit upload.
set -euo pipefail
project=$(cd "$(dirname "$0")/.." && pwd)
stage=$(mktemp -d /var/lib/persona-build.XXXXXX)
install -d "$stage/backend"
cp "$project/backend/Dockerfile" "$project/backend/requirements-prod.txt" "$project/backend/run.py" "$stage/backend/"
cp -r "$project/backend/app" "$stage/backend/app"
cat > "$stage/compose.override.yml" <<EOF
services:
  backend:
    build:
      context: $stage/backend
EOF
printf '%s\n' "$stage/compose.override.yml"
