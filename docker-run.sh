#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "Building and starting Python CLI server (port 8048)..."
docker compose up --build "$@"
