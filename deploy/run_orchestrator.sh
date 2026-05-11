#!/usr/bin/env bash
# Linux equivalent of scheduling/run_orchestrator.ps1.
# Invoked by agent-orchestrator.service.
#
# 1. Halt-flag short-circuit BEFORE venv activation (matches Windows wrapper).
# 2. Activate .venv and run python orchestrator.py.
# 3. After the run, schedule the next invocation as a transient systemd one-shot
#    using state/next_run.json. Daily fallback timer remains in place if this
#    rescheduling step fails.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/agent-trading}"
NEXT_RUN_FILE="$REPO_DIR/state/next_run.json"
HALT_FLAG="$REPO_DIR/state/halt.flag"

cd "$REPO_DIR"

if [ -f "$HALT_FLAG" ]; then
    echo "halt.flag present at $HALT_FLAG — refusing to run."
    exit 0
fi

[ -f .venv/bin/activate ] || { echo "No .venv at $REPO_DIR/.venv — re-run install.sh."; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate

set +e
python orchestrator.py
EXIT_CODE=$?
set -e

# Re-schedule from state/next_run.json regardless of exit code, so a transient
# failure does not lose the recurring trigger. Daily fallback already handles
# the worst case via agent-orchestrator.timer's OnCalendar entry.
if [ -f "$NEXT_RUN_FILE" ] && command -v jq >/dev/null 2>&1; then
    NEXT_AT=$(jq -r '.next_run_at // empty' "$NEXT_RUN_FILE" 2>/dev/null || true)
    if [ -n "$NEXT_AT" ]; then
        # Convert ISO-8601 to a value systemd-run accepts. systemd accepts
        # "yyyy-mm-dd HH:MM:SS" UTC; jq output is "yyyy-mm-ddTHH:MM:SSZ".
        SD_TIME=$(echo "$NEXT_AT" | sed 's/T/ /; s/Z$//')

        # Defensive: refuse to schedule into the past. systemd-run errors out
        # with a confusing message and the daily fallback kicks in anyway, so
        # we'd rather log a clear reason here.
        NEXT_EPOCH=$(date -u -d "$SD_TIME UTC" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date -u +%s)
        if [ "$NEXT_EPOCH" -le "$NOW_EPOCH" ]; then
            echo "next_run_at $NEXT_AT is not strictly in the future; daily fallback will run." >&2
        elif systemd-run --on-calendar="$SD_TIME UTC" \
                       --unit="agent-orchestrator-next-$(date -u +%s)" \
                       /bin/systemctl start agent-orchestrator.service \
                       >/dev/null 2>&1; then
            echo "Next run scheduled at $SD_TIME UTC."
        else
            echo "systemd-run rejected $SD_TIME UTC; daily fallback will run." >&2
        fi
    fi
fi

exit "$EXIT_CODE"
