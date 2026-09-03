#!/usr/bin/env bash
# Market Analyst - macOS / Linux launcher
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] Creating the virtual environment…"
  python3 -m venv .venv
  echo "[2/3] Installing dependencies…"
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -e . -q
else
  echo "[1/3] Environment is ready."
fi

echo "[3/3] Starting the dashboard on http://127.0.0.1:8000"
exec .venv/bin/analyst serve --host 127.0.0.1 --port 8000 "$@"
