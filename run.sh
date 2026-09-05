#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

CONFIG_PATH="${HUB_CONFIG_PATH:-$(pwd)/config/local.json}"
PORT="8765"
if [[ -f "$CONFIG_PATH" ]]; then
  PORT="$(jq -r '.agent_hub.port // 8765' "$CONFIG_PATH" 2>/dev/null || echo "8765")"
fi

exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
