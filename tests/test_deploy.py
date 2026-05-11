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


def test_run_orchestrator_uses_state_next_run_for_rescheduling():
    raw = (DEPLOY / "run_orchestrator.sh").read_text(encoding="utf-8")
    assert "state/next_run.json" in raw
    assert "systemd-run" in raw


def test_run_orchestrator_refuses_past_next_run_time():
    """systemd-run errors if --on-calendar is in the past. The wrapper now
    short-circuits and logs a clear reason instead of letting systemd-run
    emit a confusing error."""
    raw = (DEPLOY / "run_orchestrator.sh").read_text(encoding="utf-8")
    # Comparison of NEXT_EPOCH ≤ NOW_EPOCH means "skip if not strictly future"
    assert "NEXT_EPOCH" in raw and "NOW_EPOCH" in raw
    assert "in the future" in raw


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
