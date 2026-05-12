#!/usr/bin/env bash
# Root-level dynamic scheduler for agent-orchestrator.
#
# Why this exists: the orchestrator's meta-scheduler stage chooses the next
# cycle's time and writes it to state/next_run.json. The original design
# scheduled that via `systemd-run --on-calendar=...` from the orchestrator
# wrapper, but that wrapper runs as the unprivileged `agent` user (with
# NoNewPrivileges=true), so it can't create system-level transient timers
# or start system services — its rescheduling attempts errored silently
# and the only thing that actually fired was the daily 13:30 UTC fallback
# in agent-orchestrator.timer.
#
# This service is the fix: a small root-level daemon that polls
# state/next_run.json once per minute, and when the meta-scheduler's
# `next_run_at` is due, starts agent-orchestrator.service. The
# orchestrator continues to run as `agent` (the scheduler only needs root
# to call `systemctl start`).
#
# Behaviour:
#   - Polls every POLL_SECONDS (default 60).
#   - Reads next_run.json each tick; if `next_run_at` <= now AND we
#     haven't already fired for that exact target AND the orchestrator
#     isn't currently running AND state/halt.flag is absent — fires
#     `systemctl start agent-orchestrator.service` (which is async, so
#     the start returns immediately and the orchestrator runs in the
#     background).
#   - Records `state/scheduler_last_fired.txt` containing the
#     `next_run_at` we just triggered, so subsequent ticks don't refire
#     the same target while the orchestrator is mid-run.
#   - On any error (malformed JSON, missing file, halt flag, etc.):
#     logs and continues — never exits, so the daily fallback timer in
#     agent-orchestrator.timer still acts as the safety net.
#   - Honours SIGTERM (systemd stop) by exiting cleanly between ticks.
set -uo pipefail
# Note: deliberately NOT using `set -e` — we want the loop to survive
# every kind of parse failure or transient systemctl error.

REPO_DIR="${REPO_DIR:-/opt/agent-trading}"
POLL_SECONDS="${POLL_SECONDS:-60}"
NEXT_RUN_FILE="$REPO_DIR/state/next_run.json"
LAST_FIRED_FILE="$REPO_DIR/state/scheduler_last_fired.txt"
HALT_FLAG="$REPO_DIR/state/halt.flag"
ORCH_SERVICE="agent-orchestrator.service"

log() { printf '[%(%Y-%m-%dT%H:%M:%SZ)T] scheduler: %s\n' -1 "$*"; }

# Set up SIGTERM trap so `systemctl stop agent-scheduler.service` exits
# without leaving a 60-second sleep hanging.
trap 'log "received SIGTERM, exiting cleanly"; exit 0' TERM INT

log "starting; polling every ${POLL_SECONDS}s; watching ${NEXT_RUN_FILE}"

while true; do
    # ---- halt-flag short-circuit (matches orchestrator wrapper) ----
    if [ -f "$HALT_FLAG" ]; then
        log "halt.flag present at $HALT_FLAG — not firing"
        sleep "$POLL_SECONDS"
        continue
    fi

    if [ ! -f "$NEXT_RUN_FILE" ]; then
        # No schedule yet (first install before any run). Daily fallback
        # in agent-orchestrator.timer covers this — we just wait.
        sleep "$POLL_SECONDS"
        continue
    fi

    if ! command -v jq >/dev/null 2>&1; then
        log "jq not installed — cannot parse next_run.json; sleeping"
        sleep "$POLL_SECONDS"
        continue
    fi

    NEXT_AT=$(jq -r '.next_run_at // empty' "$NEXT_RUN_FILE" 2>/dev/null || true)
    if [ -z "$NEXT_AT" ]; then
        # next_run.json present but missing/null next_run_at — bad shape,
        # daily fallback still covers us, just skip this tick.
        sleep "$POLL_SECONDS"
        continue
    fi

    NEXT_EPOCH=$(date -u -d "${NEXT_AT/Z/} UTC" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date -u +%s)

    if [ "$NEXT_EPOCH" -eq 0 ]; then
        log "could not parse next_run_at='$NEXT_AT'; skipping"
        sleep "$POLL_SECONDS"
        continue
    fi

    if [ "$NEXT_EPOCH" -gt "$NOW_EPOCH" ]; then
        # Not due yet.
        sleep "$POLL_SECONDS"
        continue
    fi

    # Have we already fired for this exact target? If so, the orchestrator
    # is mid-run (or completed but hasn't yet written a fresher
    # next_run.json with a later timestamp). Don't refire.
    LAST_FIRED=""
    [ -f "$LAST_FIRED_FILE" ] && LAST_FIRED=$(cat "$LAST_FIRED_FILE" 2>/dev/null || echo "")
    if [ "$LAST_FIRED" = "$NEXT_AT" ]; then
        sleep "$POLL_SECONDS"
        continue
    fi

    # Don't queue duplicates if the orchestrator is still running from a
    # previous trigger (manual or otherwise). is-active returns
    # "active" / "activating" while a oneshot is running.
    ORCH_STATE=$(systemctl is-active "$ORCH_SERVICE" 2>&1 || true)
    if [ "$ORCH_STATE" = "active" ] || [ "$ORCH_STATE" = "activating" ]; then
        log "orchestrator is currently $ORCH_STATE; deferring fire for next_run_at=$NEXT_AT"
        sleep "$POLL_SECONDS"
        continue
    fi

    # All clear — fire it.
    log "next_run_at=$NEXT_AT is due (was ${LAST_FIRED:-none}); starting $ORCH_SERVICE"
    if systemctl start "$ORCH_SERVICE"; then
        # Record what we fired so we don't refire on next tick.
        echo -n "$NEXT_AT" > "$LAST_FIRED_FILE"
    else
        log "systemctl start failed; will retry next tick"
    fi

    sleep "$POLL_SECONDS"
done
