#!/usr/bin/env bash
# Start MrSimplex (Linux / macOS).
# First run creates a virtual environment and installs dependencies.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
  echo "[setup] creating virtual environment..."
  "$PY" -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  echo "[setup] installing dependencies..."
  ./.venv/bin/pip install -r requirements.txt
fi

# Pass any args through (e.g. ./run.sh --list-folders)
exec ./.venv/bin/python bot.py "$@"
