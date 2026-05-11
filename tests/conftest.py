"""Test fixtures — redirect lib.state paths into a temp dir per test."""
from __future__ import annotations

from pathlib import Path

import pytest

import lib.state as state_mod


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch) -> Path:
    """Give each test its own isolated state/ directory."""
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    (sd / "runs").mkdir()
    monkeypatch.setattr(state_mod, "STATE_DIR", sd)
    monkeypatch.setattr(state_mod, "RUNS_DIR", sd / "runs")
    monkeypatch.setattr(state_mod, "HALT_FLAG", sd / "halt.flag")
    monkeypatch.setattr(state_mod, "DECISIONS_LOG", sd / "decisions.jsonl")
    monkeypatch.setattr(state_mod, "COSTS_LOG", sd / "costs.jsonl")
    monkeypatch.setattr(state_mod, "CURRENT_PORTFOLIO", sd / "current_portfolio.json")
    monkeypatch.setattr(state_mod, "NEXT_RUN", sd / "next_run.json")
    monkeypatch.setattr(state_mod, "NAV_HISTORY_LOG", sd / "nav_history.jsonl")
    return sd
