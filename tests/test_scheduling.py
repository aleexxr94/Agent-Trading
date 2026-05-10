"""Phase 6 — Task Scheduler XML + PowerShell file shape tests."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHED = ROOT / "scheduling"
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

REQUIRED_PLACEHOLDERS = ("{{REPO_ROOT}}", "{{USER_SID}}", "{{USER_UPN}}")


@pytest.mark.parametrize("name", ["orchestrator_task.xml", "monitor_task.xml"])
def test_task_xml_wellformed_and_has_placeholders(name):
    path = SCHED / name
    raw = path.read_text(encoding="utf-8")
    # Well-formedness: must parse with the standard library.
    tree = ET.parse(path)
    root = tree.getroot()
    assert root.tag == f"{NS}Task"
    triggers = root.find(f"{NS}Triggers")
    assert triggers is not None and len(list(triggers)) >= 1
    args = root.find(f"{NS}Actions/{NS}Exec/{NS}Arguments")
    assert args is not None and "scheduling" in args.text
    # Placeholders register_task.ps1 expects to substitute.
    for ph in REQUIRED_PLACEHOLDERS:
        assert ph in raw, f"{name} missing placeholder {ph}"


@pytest.mark.parametrize("name", [
    "register_task.ps1",
    "unregister_task.ps1",
    "run_orchestrator.ps1",
    "run_monitor.ps1",
])
def test_powershell_scripts_have_strict_mode(name):
    """All PowerShell wrappers should fail closed on undefined vars + errors."""
    raw = (SCHED / name).read_text(encoding="utf-8")
    assert '$ErrorActionPreference = "Stop"' in raw
    assert "Set-StrictMode -Version Latest" in raw


def test_register_task_ps1_substitutes_placeholders():
    raw = (SCHED / "register_task.ps1").read_text(encoding="utf-8")
    for ph in REQUIRED_PLACEHOLDERS:
        assert f'"{ph}"' in raw, f"register_task.ps1 must substitute {ph}"


def test_run_orchestrator_checks_halt_flag_first():
    """run_orchestrator.ps1 must short-circuit when halt.flag is present."""
    raw = (SCHED / "run_orchestrator.ps1").read_text(encoding="utf-8")
    halt_idx = raw.find("halt.flag")
    venv_idx = raw.find("Activate.ps1")
    assert 0 < halt_idx < venv_idx, "halt.flag check must precede venv activation"


def test_unregister_handles_missing_tasks():
    raw = (SCHED / "unregister_task.ps1").read_text(encoding="utf-8")
    assert "ErrorAction SilentlyContinue" in raw  # tolerate already-gone tasks
