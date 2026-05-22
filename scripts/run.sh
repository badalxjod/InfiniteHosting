#!/usr/bin/env bash
# scripts/run.sh — Start HostingBot (with optional venv auto-creation)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$ROOT_DIR/.venv"

cd "$ROOT_DIR"

# Create venv if missing
if [ ! -d "$VENV_DIR" ]; then
  echo "[run.sh] Creating virtual environment…"
  python3 -m venv "$VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"

# Install / upgrade dependencies
echo "[run.sh] Installing dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Start the bot
echo "[run.sh] Starting HostingBot…"
exec python3 main.py
