#!/usr/bin/env bash
# ── BrighterNepal Chat Service Launcher ──────────────────────────────────
# Usage: ./run.sh

set -e
cd "$(dirname "$0")"

# Create virtualenv if not present
if [ ! -d "venv" ]; then
  echo "[chat] Creating virtualenv..."
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo "[chat] Starting Flask-SocketIO chat service (eventlet, port 5001)"
python app.py
