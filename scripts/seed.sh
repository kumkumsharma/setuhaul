#!/usr/bin/env bash
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

# Prefer a working 3.12 venv (.venv312) over a broken system .venv
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

mkdir -p data/runtime
export PYTHONPATH="$ROOT/apps/api"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./data/runtime/setuhaul.db}"
export REDIS_URL="${REDIS_URL:-fakeredis://}"
export SCENARIO_NOW="${SCENARIO_NOW:-2026-08-11T17:25:00+05:30}"

python -m app.seed.load
echo "Database seeded at $DATABASE_URL (venv=$VENV_DIR)"
