#!/usr/bin/env bash
# Monitor wrapper for the Linux VPS deployment.
# Invoked by agent-monitor.service.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/agent-trading}"
HALT_FLAG="$REPO_DIR/state/halt.flag"

cd "$REPO_DIR"

if [ -f "$HALT_FLAG" ]; then
    echo "halt.flag present — monitor exiting cleanly."
    exit 0
fi

[ -f .venv/bin/activate ] || { echo "No .venv at $REPO_DIR/.venv — re-run install.sh."; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate

exec python monitor.py
