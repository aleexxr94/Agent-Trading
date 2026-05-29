#!/usr/bin/env bash
# Orchestrator wrapper for the Linux VPS deployment.
# Invoked by agent-orchestrator.service.
#
# 1. Halt-flag short-circuit BEFORE venv activation.
# 2. Activate .venv and run python orchestrator.py.
#
# Dynamic scheduling (firing the NEXT cycle from state/next_run.json) is
# NOT handled here — this wrapper runs as the unprivileged `agent` user
# with NoNewPrivileges=true, so `systemd-run` could not create system
# timers or start system services and was silently erroring. That
# responsibility now lives in agent-scheduler.service (a small root-level
# daemon that polls next_run.json). See deploy/run_scheduler.sh.
#
# The daily-fallback OnCalendar trigger in agent-orchestrator.timer is
# unchanged and still acts as the safety net.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/agent-trading}"
HALT_FLAG="$REPO_DIR/state/halt.flag"

cd "$REPO_DIR"

if [ -f "$HALT_FLAG" ]; then
    echo "halt.flag present at $HALT_FLAG — refusing to run."
    exit 0
fi

[ -f .venv/bin/activate ] || { echo "No .venv at $REPO_DIR/.venv — re-run install.sh."; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate

exec python orchestrator.py
