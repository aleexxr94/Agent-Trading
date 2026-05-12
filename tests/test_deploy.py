"""Phase 8 — Linux deploy file shape tests.

Mirrors test_scheduling.py but for the systemd / bash deploy artifacts.
Locks halt-flag-precedes-venv ordering and required placeholder presence.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

REQUIRED_UNIT_PLACEHOLDERS = ("{{REPO_DIR}}", "{{AGENT_USER}}")


@pytest.mark.parametrize("name", [
    "install.sh", "run_orchestrator.sh", "run_monitor.sh",
])
def test_bash_scripts_use_strict_mode(name):
    raw = (DEPLOY / name).read_text(encoding="utf-8")
    assert "set -euo pipefail" in raw, f"{name} must use 'set -euo pipefail'"
    assert raw.startswith("#!/usr/bin/env bash"), f"{name} must use the env shebang"


@pytest.mark.parametrize("name", ["run_orchestrator.sh", "run_monitor.sh"])
def test_wrapper_halt_check_precedes_venv(name):
    """Mirrors test_scheduling.py — halt.flag check must short-circuit before
    activating the venv, otherwise we waste a python startup on a halted run."""
    raw = (DEPLOY / name).read_text(encoding="utf-8")
    halt_idx = raw.find("halt.flag")
    venv_idx = raw.find(".venv/bin/activate")
    assert 0 < halt_idx < venv_idx, f"{name}: halt.flag check must precede venv activation"


@pytest.mark.parametrize("name", [
    "agent-orchestrator.service",
    "agent-orchestrator.timer",
    "agent-monitor.service",
    "agent-monitor.timer",
    "agent-dashboard.service",
])
def test_systemd_units_present(name):
    assert (DEPLOY / "systemd" / name).exists()


@pytest.mark.parametrize("name", [
    "agent-orchestrator.service",
    "agent-monitor.service",
    "agent-dashboard.service",
])
def test_service_units_have_required_placeholders(name):
    raw = (DEPLOY / "systemd" / name).read_text(encoding="utf-8")
    for ph in REQUIRED_UNIT_PLACEHOLDERS:
        assert ph in raw, f"{name} missing placeholder {ph}"
    assert "User={{AGENT_USER}}" in raw
    assert "WorkingDirectory={{REPO_DIR}}" in raw


@pytest.mark.parametrize("name", [
    "agent-orchestrator.service",
    "agent-monitor.service",
    "agent-dashboard.service",
])
def test_service_units_are_hardened(name):
    raw = (DEPLOY / "systemd" / name).read_text(encoding="utf-8")
    for required in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "ReadWritePaths={{REPO_DIR}}/state",
        "ReadOnlyPaths={{REPO_DIR}}",
    ):
        assert required in raw, f"{name} missing hardening directive {required!r}"


def test_orchestrator_and_monitor_units_check_halt_flag():
    """systemd should refuse to start the unit when halt.flag exists, in
    addition to the wrapper's runtime check."""
    for unit in ("agent-orchestrator.service", "agent-monitor.service"):
        raw = (DEPLOY / "systemd" / unit).read_text(encoding="utf-8")
        assert "ConditionPathExists=!{{REPO_DIR}}/state/halt.flag" in raw, (
            f"{unit} missing halt.flag ConditionPathExists guard"
        )


def test_dashboard_binds_localhost_only():
    """Dashboard must NOT expose 0.0.0.0; phone access goes via Tailscale."""
    raw = (DEPLOY / "systemd" / "agent-dashboard.service").read_text(encoding="utf-8")
    assert "--server.address 127.0.0.1" in raw
    assert "0.0.0.0" not in raw


def test_install_sh_seeds_env_at_mode_600():
    raw = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "install -m 600" in raw and ".env.example" in raw


def test_orchestrator_timer_has_daily_fallback():
    raw = (DEPLOY / "systemd" / "agent-orchestrator.timer").read_text(encoding="utf-8")
    assert "OnCalendar=" in raw
    assert "Persistent=true" in raw  # catches up missed runs after downtime


def test_monitor_timer_market_hours_only():
    raw = (DEPLOY / "systemd" / "agent-monitor.timer").read_text(encoding="utf-8")
    # Should target hours 13–21 UTC at 15-min step; not every minute.
    assert "13..21" in raw and "/15" in raw


def test_run_orchestrator_does_not_self_reschedule():
    """The orchestrator wrapper used to try `systemd-run --on-calendar=...`
    after each run to schedule the next one. That broke because the wrapper
    runs as the unprivileged agent user (NoNewPrivileges=true) — systemd-run
    silently failed and the daily-fallback OnCalendar was the only thing
    actually firing.

    Dynamic scheduling now lives in agent-scheduler.service /
    deploy/run_scheduler.sh (root-level poller). The wrapper's executable
    lines must not call systemd-run or read next_run.json. (Comment lines
    that explain WHY this responsibility moved are fine.)
    """
    raw = (DEPLOY / "run_orchestrator.sh").read_text(encoding="utf-8")
    # Strip comment lines + blank lines so we only check the active code.
    code_lines = [
        line for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "systemd-run" not in code, (
        "run_orchestrator.sh must not call systemd-run — scheduling lives "
        "in run_scheduler.sh / agent-scheduler.service now"
    )
    assert "next_run.json" not in code, (
        "run_orchestrator.sh no longer reads next_run.json — that's the "
        "scheduler service's job"
    )


def test_run_scheduler_exists_and_polls_next_run():
    """agent-scheduler.service runs as root and polls state/next_run.json
    on a fixed cadence, firing `systemctl start agent-orchestrator.service`
    when the meta-scheduler's chosen time arrives. This is the privilege
    boundary that the old in-orchestrator systemd-run approach could not
    cross."""
    script = DEPLOY / "run_scheduler.sh"
    assert script.exists(), "deploy/run_scheduler.sh missing"
    raw = script.read_text(encoding="utf-8")
    assert "state/next_run.json" in raw
    assert "agent-orchestrator.service" in raw
    assert "systemctl start" in raw
    # Idempotency safeguard: don't refire while orchestrator is mid-run,
    # don't refire for a target we already fired.
    assert "scheduler_last_fired.txt" in raw
    assert "is-active" in raw
    # Halt flag honoured (matches orchestrator wrapper convention)
    assert "halt.flag" in raw


def test_agent_scheduler_unit_runs_as_root():
    """The scheduler service must run as root because it calls
    `systemctl start agent-orchestrator.service` (starting a system service
    requires PolicyKit-elevated permission). The orchestrator itself still
    runs as the unprivileged agent user."""
    raw = (DEPLOY / "systemd" / "agent-scheduler.service").read_text(encoding="utf-8")
    assert "User=root" in raw
    assert "Type=simple" in raw         # long-running daemon
    assert "Restart=always" in raw      # survives transient failures
    assert "run_scheduler.sh" in raw


def test_install_sh_installs_scheduler_unit():
    """install.sh must drop agent-scheduler.service into /etc/systemd/system
    alongside the other units."""
    raw = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "agent-scheduler.service" in raw


@pytest.mark.parametrize("name", [
    "agent-orchestrator.service",
    "agent-monitor.service",
    "agent-dashboard.service",
])
def test_service_units_redirect_home_to_tmp(name):
    """ProtectHome=true hides /home; libraries that read $HOME/.* on startup
    (streamlit secrets.toml, anthropic SDK config, yfinance cache) need a
    writable HOME or they crash. Point HOME at the per-service PrivateTmp."""
    raw = (DEPLOY / "systemd" / name).read_text(encoding="utf-8")
    assert "Environment=HOME=/tmp" in raw, (
        f"{name}: redirect HOME so streamlit/anthropic/yfinance can write "
        f"under PrivateTmp instead of hitting the ProtectHome wall"
    )
