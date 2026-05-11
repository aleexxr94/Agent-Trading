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


def test_position_table_rows_without_marks_leaves_pnl_blank(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio)
    for r in rows:
        assert r["Mark"] is None
        assert r["Gross P&L"] is None
        assert r["Net P&L"] is None


def test_position_table_rows_etf_pnl_with_marks(tmp_state):
    """ETF mark above cost → positive gross; net subtracts modelled costs."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio, marks={"TQQQ": 80.0})
    r = rows[0]
    assert r["Mark"] == 80.0
    # 4 shares × (80 - 70) = $40 gross
    assert r["Gross P&L"] == pytest.approx(40.0)
    # Net is below gross by some modelled round-trip cost.
    assert r["Net P&L"] < r["Gross P&L"]


def _option_pos():
    return {
        "kind": "option", "underlying": "SPY", "type": "call",
        "strike": 530.0, "expiry": "2026-06-19", "dte": 40,
        "contracts": 1, "premium_paid": 6.50,
        "greeks": {"delta": 0.45, "gamma": 0.02, "theta": -0.04, "vega": 0.18,
                    "iv": 0.18, "iv_percentile": 35},
        "entry_thesis": "x", "kill_conditions": {"max_loss_pct": 100},
        "position_pct": 5.0,
    }


def test_position_table_rows_option_pnl_uses_synthetic_key(tmp_state):
    """Option marks use 'underlying|strike|expiry|type' key, same as monitor.py."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},
        marks={"SPY|530.0|2026-06-19|call": 8.00},
    )
    r = rows[0]
    assert r["Mark"] == 8.00
    # 1 contract × 100 × (8.00 - 6.50) = $150 gross
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_position_table_rows_option_pnl_falls_back_to_osi_key(tmp_state):
    """marks_from_broker currently keys options by OSI symbol. Until that
    aligns with the synthetic key, the dashboard must accept either so live
    option marks actually flow into the Mark / Gross / Net columns.

    Regression test for the P1 Codex flagged on PR #21: without this fallback,
    option P&L stays blank in any portfolio holding options.
    """
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},
        marks={"SPY260619C00530000": 8.00},
    )
    r = rows[0]
    assert r["Mark"] == 8.00
    assert r["Gross P&L"] == pytest.approx(150.0)


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


# ---------- token + cost aggregation ----------


def test_total_token_cost_empty(tmp_state):
    t = dd.total_token_cost()
    assert t["calls"] == 0
    assert t["total_tokens"] == 0
    assert t["cost_usd"] == 0.0


def test_total_token_cost_aggregates(tmp_state):
    state.append_cost({
        "run_id": "r1", "stage": "screen", "model": "claude-haiku-4-5",
        "cost_usd": 0.04, "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 800,
        "at": "2026-05-10T12:00:00Z",
    })
    state.append_cost({
        "run_id": "r2", "stage": "scenarios", "model": "claude-sonnet-4-6",
        "cost_usd": 0.18, "input_tokens": 500, "output_tokens": 1500,
        "cache_creation_input_tokens": 200, "cache_read_input_tokens": 0,
        "at": "2026-04-15T09:30:00Z",
    })
    t = dd.total_token_cost()
    assert t["calls"] == 2
    assert t["input_tokens"] == 1500
    assert t["output_tokens"] == 1700
    assert t["cache_creation_input_tokens"] == 200
    assert t["cache_read_input_tokens"] == 800
    assert t["total_tokens"] == 1500 + 1700 + 200 + 800
    assert t["cost_usd"] == pytest.approx(0.22)


def test_cost_by_month_buckets_correctly(tmp_state):
    rows = [
        ("2026-05-10T12:00:00Z", 0.10, 1000),
        ("2026-05-29T23:00:00Z", 0.20, 2000),
        ("2026-04-15T09:30:00Z", 0.30, 3000),
        ("2026-03-01T00:00:01Z", 0.05, 500),
    ]
    for at, cost, tokens in rows:
        state.append_cost({
            "run_id": "x", "stage": "s", "model": "m",
            "cost_usd": cost, "input_tokens": tokens,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "at": at,
        })
    by_month = dd.cost_by_month()
    assert [b["month"] for b in by_month] == ["2026-03", "2026-04", "2026-05"]
    may = next(b for b in by_month if b["month"] == "2026-05")
    assert may["calls"] == 2
    assert may["cost_usd"] == pytest.approx(0.30)
    assert may["total_tokens"] == 3000


def test_cost_by_month_empty(tmp_state):
    assert dd.cost_by_month() == []


def test_try_load_broker_marks_no_keys_returns_empty(tmp_state, monkeypatch):
    """Without ALPACA_API_KEY / SECRET in env, AlpacaBroker init raises and
    the dashboard helper must absorb the failure so the page still renders."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert dd.try_load_broker_marks() == {}


def test_load_nav_history_round_trip(tmp_state):
    assert dd.load_nav_history() == []
    state.append_nav({"run_id": "r1", "at": state.utcnow_iso(), "nav_usd": 2500.0})
    state.append_nav({"run_id": "r2", "at": state.utcnow_iso(), "nav_usd": 2520.0})
    hist = dd.load_nav_history()
    assert len(hist) == 2
    assert hist[1]["nav_usd"] == 2520.0
