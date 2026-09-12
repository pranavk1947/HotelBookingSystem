#!/usr/bin/env bash
# Boots the Group Proposal Agent: venv -> deps -> uvicorn.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"

# Needs 3.10+ (PEP 604 unions are resolved at runtime by Pydantic). macOS ships
# 3.9 as /usr/bin/python3, so pick the first interpreter that actually qualifies
# rather than trusting whatever `python3` happens to be.
find_python() {
  for candidate in "${PYTHON_BIN:-}" python3 python3.13 python3.12 python3.11 python3.10; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if [ ! -d "$VENV" ]; then
  if ! PYTHON_BIN="$(find_python)"; then
    echo "ERROR: Python 3.10 or newer is required." >&2
    echo "       Found: $(python3 -V 2>&1 || echo 'no python3 on PATH')" >&2
    echo "       Install one (brew install python@3.12) or set PYTHON_BIN=/path/to/python3.12" >&2
    exit 1
  fi
  echo "==> Creating virtualenv at .venv using $("$PYTHON_BIN" -V 2>&1)"
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
