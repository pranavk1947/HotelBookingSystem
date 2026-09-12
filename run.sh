#!/usr/bin/env bash
# Boots the Group Proposal Agent: venv -> deps -> uvicorn.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="$ROOT/.venv"

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtualenv at .venv"
  "$PYTHON_BIN" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing dependencies"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

if [ ! -f "$ROOT/.env" ]; then
  echo "==> No .env found; copying .env.example -> .env (add your ANTHROPIC_API_KEY)"
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

cd "$ROOT/server"
exec python -m uvicorn src.app:app --host 127.0.0.1 --port "${PORT:-8000}"
