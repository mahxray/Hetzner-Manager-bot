#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null || { echo 'Install Python 3 first.' >&2; exit 1; }
command -v docker >/dev/null || { echo 'Install Docker Engine and the Compose plugin: https://docs.docker.com/engine/install/' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
if [[ ! -f .env ]]; then
  python3 scripts/setup.py
fi
chmod 600 .env
docker compose up -d --build
echo 'Started. In Telegram open your bot and send /start. Logs: docker compose logs --tail=50 bot'
