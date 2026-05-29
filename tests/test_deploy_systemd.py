"""Linux VPS / systemd deployment shape tests.

Replaces the old test_scheduling.py (which validated the removed Windows Task
Scheduler XML + PowerShell wrappers). Linux VPS + systemd is now the sole
supported production runtime — see CLAUDE.md §Runtime and deploy/README.md.

These tests lock the deploy artifacts' shape: required wrappers + unit
templates exist, wrappers use safe shell settings and check halt.flag before
touching the venv/Python, and the systemd units carry the expected
placeholders and hardening posture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SYSTEMD = DEPLOY / "systemd"

REQUIRED_WRAPPERS = (
    "run_orchestrator.sh",
    "run_monitor.sh",
    "run_scheduler.sh",
)

REQUIRED_UNITS = (
    "agent-orchestrator.service",
    "agent-orchestrator.timer",
    "agent-monitor.service",
    "agent-monitor.timer",
    "agent-dashboard.service",
    "agent-scheduler.service",
)

REQUIRED_UNIT_PLACEHOLDERS = ("{{REPO_DIR}}", "{{AGENT_USER}}")


def test_no_windows_scheduling_dir():
    """The Windows Task Scheduler runtime directory must be gone — Linux VPS
    + systemd is the sole supported runtime."""
    assert not (ROOT / "scheduling").exists(), (
        "scheduling/ (Windows Task Scheduler runtime) should be removed"
    )


@pytest.mark.parametrize("name", REQUIRED_WRAPPERS)
def test_deploy_wrappers_exist(name):
    assert (DEPLOY / name).is_file(), f"deploy/{name} missing"


@pytest.mark.parametrize("name", REQUIRED_UNITS)
def test_systemd_unit_templates_exist(name):
    assert (SYSTEMD / name).is_file(), f"deploy/systemd/{name} missing"


@pytest.mark.parametrize("name", REQUIRED_WRAPPERS)
def test_wrappers_use_safe_shell_settings(name):
    """All wrappers use the env shebang + safe unset/pipefail settings.

    The two one-shot wrappers (orchestrator, monitor) use full `set -euo
    pipefail`. run_scheduler.sh is a long-running daemon loop that
    deliberately omits `set -e` so a transient per-tick failure can't kill
    the loop — `set -uo pipefail` is the appropriate safe setting there.
    """
    raw = (DEPLOY / name).read_text(encoding="utf-8")
    assert raw.startswith("#!/usr/bin/env bash"), f"{name} must use the env shebang"
    if name == "run_scheduler.sh":
        assert "set -uo pipefail" in raw, f"{name} must use 'set -uo pipefail'"
    else:
        assert "set -euo pipefail" in raw, f"{name} must use 'set -euo pipefail'"


@pytest.mark.parametrize("name", ["run_orchestrator.sh", "run_monitor.sh"])
def test_orchestrator_monitor_wrappers_check_halt_before_venv(name):
    """halt.flag must short-circuit BEFORE activating the venv / invoking
    Python, otherwise a halted run still pays a Python startup."""
    raw = (DEPLOY / name).read_text(encoding="utf-8")
    halt_idx = raw.find("halt.flag")
    venv_idx = raw.find(".venv/bin/activate")
    assert 0 < halt_idx < venv_idx, (
        f"{name}: halt.flag check must precede venv activation"
    )


@pytest.mark.parametrize("name", [
    "agent-orchestrator.service",
    "agent-monitor.service",
    "agent-dashboard.service",
])
def test_service_units_have_placeholders(name):
    raw = (SYSTEMD / name).read_text(encoding="utf-8")
    for ph in REQUIRED_UNIT_PLACEHOLDERS:
        assert ph in raw, f"{name} missing placeholder {ph}"
    assert "WorkingDirectory={{REPO_DIR}}" in raw
    assert "User={{AGENT_USER}}" in raw


@pytest.mark.parametrize("name", [
    "agent-orchestrator.service",
    "agent-monitor.service",
    "agent-dashboard.service",
])
def test_service_units_retain_hardening(name):
    """systemd units keep their existing safety posture: no privilege
    escalation, read-only repo, writable state/."""
    raw = (SYSTEMD / name).read_text(encoding="utf-8")
    for required in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ReadOnlyPaths={{REPO_DIR}}",
        "ReadWritePaths={{REPO_DIR}}/state",
    ):
        assert required in raw, f"{name} missing hardening directive {required!r}"


def test_orchestrator_and_monitor_units_gate_on_halt_flag():
    """systemd refuses to start the unit when halt.flag exists, on top of the
    wrapper's runtime check."""
    for unit in ("agent-orchestrator.service", "agent-monitor.service"):
        raw = (SYSTEMD / unit).read_text(encoding="utf-8")
        assert "ConditionPathExists=!{{REPO_DIR}}/state/halt.flag" in raw, (
            f"{unit} missing halt.flag ConditionPathExists guard"
        )


def test_dashboard_binds_localhost_only():
    """Dashboard binds 127.0.0.1 only — phone access is via Tailscale or an
    SSH tunnel, never a public 0.0.0.0 bind."""
    raw = (SYSTEMD / "agent-dashboard.service").read_text(encoding="utf-8")
    assert "--server.address 127.0.0.1" in raw
    assert "0.0.0.0" not in raw
