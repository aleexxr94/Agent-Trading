"""Pure-data-layer tests for the dashboard. No streamlit dependency required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import dashboard_data as dd
from lib import state


FIXTURE = Path(__file__).parent / "fixtures" / "portfolio.json"


def test_load_portfolio_falls_back_to_fixture(tmp_state):
    p, src = dd.load_portfolio()
    assert src == "fixture"
    assert 8 <= len(p["positions"]) <= 12


def test_load_portfolio_prefers_live(tmp_state):
    state.write_json(state.CURRENT_PORTFOLIO, json.loads(FIXTURE.read_text()))
    p, src = dd.load_portfolio()
    assert src == "live"


def test_load_portfolio_seed_overrides_fixture(tmp_state):
    seed = state.STATE_DIR / "seed_portfolio.json"
    state.write_json(seed, json.loads(FIXTURE.read_text()))
    _, src = dd.load_portfolio()
    assert src == "seed"


def test_position_table_rows_etf_and_option_columns(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio)
    kinds = {r["Kind"] for r in rows}
    assert kinds == {"ETF", "OPT"}
    opt_row = next(r for r in rows if r["Kind"] == "OPT")
    assert "Δ" in opt_row["Greeks"]
    assert opt_row["Kill"] == "≤100%"


def test_allocation_pie_includes_cash(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    pie = dd.allocation_pie(portfolio)
    assert any(r["label"] == "Cash" for r in pie)
    assert sum(r["value"] for r in pie) == pytest.approx(100.0, abs=0.5)


def test_load_decisions_empty_log(tmp_state):
    assert dd.load_decisions() == []


def test_cost_today_zero_no_log(tmp_state):
    assert dd.cost_today_usd() == 0.0


def test_cost_today_aggregates(tmp_state):
    state.append_cost({"run_id": "r1", "stage": "screen", "model": "m", "cost_usd": 0.10, "at": state.utcnow_iso()})
    state.append_cost({"run_id": "r2", "stage": "screen", "model": "m", "cost_usd": 0.20, "at": state.utcnow_iso()})
    assert dd.cost_today_usd() == pytest.approx(0.30)


def test_empty_portfolio_when_nothing_anywhere(tmp_state, monkeypatch):
    """If the fixture file is also missing, helper returns a coherent all-cash stub."""
    monkeypatch.setattr(dd, "SEED_PORTFOLIO_FALLBACK", tmp_state / "does-not-exist.json")
    p, src = dd.load_portfolio()
    assert src == "empty"
    assert p["all_cash"] is True
    assert p["positions"] == []
