#!/usr/bin/env bash
# Apply Alembic migrations only. Never drops tables or seeds demo data.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$HOME/.local/bin/python3.12" ]]; then
    PYTHON_BIN="$HOME/.local/bin/python3.12"
  elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.12)"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

if [[ -x .venv312/bin/python ]]; then
  VENV_DIR=".venv312"
elif [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info < (3,14) else 1)'; then
  VENV_DIR=".venv"
else
  VENV_DIR=".venv312"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install -q -r apps/api/requirements.txt

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export PYTHONPATH="$ROOT/apps/api"
cd "$ROOT/apps/api"
alembic -c alembic.ini upgrade head
echo "Schema migrations applied (Alembic upgrade head)."
