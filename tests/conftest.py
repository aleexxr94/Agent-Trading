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
    monkeypatch.setattr(state_mod, "TRADES_LOG", sd / "trades.jsonl")
    monkeypatch.setattr(state_mod, "CURRENT_PORTFOLIO", sd / "current_portfolio.json")
    monkeypatch.setattr(state_mod, "NEXT_RUN", sd / "next_run.json")
    monkeypatch.setattr(state_mod, "NAV_HISTORY_LOG", sd / "nav_history.jsonl")
    monkeypatch.setattr(state_mod, "MONITOR_SHADOW_LOG", sd / "monitor_shadow.jsonl")
    monkeypatch.setattr(state_mod, "DD_HALT_FLAG", sd / "dd_halt.flag")
    monkeypatch.setattr(state_mod, "SOD_NAV_FILE", sd / "sod_nav.json")
    monkeypatch.setattr(state_mod, "COST_RESET_FLAG", sd / "cost_reset.json")
    monkeypatch.setattr(state_mod, "ALL_TIME_COST_RESET_FLAG", sd / "cost_all_time_reset.json")
    monkeypatch.setattr(state_mod, "NAV_OFFSET_FLAG", sd / "nav_offset.json")
    monkeypatch.setattr(state_mod, "NAV_MANUAL_BASELINE_FLAG", sd / "nav_manual_baseline.json")
    return sd
