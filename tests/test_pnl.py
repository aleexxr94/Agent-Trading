"""Trading-cost model + Gross / Net P&L tests."""
from __future__ import annotations

import pytest

from lib import pnl


def _etf(**over) -> dict:
    base = {
        "kind": "etf",
        "symbol": "TQQQ",
        "shares": 10,
        "avg_cost": 70.0,
        "leverage_factor": 3.0,
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 10.0,
    }
    base.update(over)
    return base


def _opt(**over) -> dict:
    base = {
        "kind": "option",
        "underlying": "SPY",
        "type": "call",
        "strike": 530.0,
        "expiry": "2026-06-19",
        "dte": 40,
        "contracts": 1,
        "premium_paid": 6.50,
        "greeks": {"delta": 0.45, "gamma": 0.02, "theta": -0.04, "vega": 0.18, "iv": 0.18, "iv_percentile": 35},
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 100},
        "position_pct": 5.0,
    }
    base.update(over)
    return base


def test_etf_round_trip_cost_is_one_leg_doubled():
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)  # $5,000 notional
    # 5 bps half-spread = $2.50 per leg
    assert leg.half_spread_usd == pytest.approx(2.50)
    assert leg.commission_usd == 0.0
    assert leg.round_trip_usd == pytest.approx(5.00)


def test_option_cost_includes_per_contract_commission():
    leg = pnl.model_option_cost(contracts=2, premium_usd=650.0)  # $1,300 notional
    # 25 bps half-spread = $3.25; commission = 2 * $0.65 = $1.30
    assert leg.half_spread_usd == pytest.approx(3.25)
    assert leg.commission_usd == pytest.approx(1.30)
    assert leg.round_trip_usd == pytest.approx(2 * (3.25 + 1.30))


def test_position_cost_etf():
    cost = pnl.model_position_cost(_etf(shares=10, avg_cost=70.0))
    # Notional $700; 5 bps RT each side → $0.35 * 2 = $0.70 spread + $0 commission
    assert cost.round_trip_usd == pytest.approx(0.70)


def test_position_cost_option_uses_dollar_premium():
    # premium_paid is per-share; *100 gives per-contract dollar premium
    cost = pnl.model_position_cost(_opt(contracts=1, premium_paid=6.50))
    # Notional = 1 * $650; 25 bps half-spread per leg = $1.625 * 2 = $3.25
    # Commission = 2 legs * 1 contract * $0.65 = $1.30
    assert cost.round_trip_usd == pytest.approx(3.25 + 1.30)


def test_position_pnl_etf_gain():
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=75.0)
    assert b.gross_pnl_usd == pytest.approx(50.0)  # 10 shares * $5
    assert b.modelled_costs_usd > 0
    assert b.net_pnl_usd == pytest.approx(b.gross_pnl_usd - b.modelled_costs_usd)


def test_position_pnl_etf_loss():
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=65.0)
    assert b.gross_pnl_usd == pytest.approx(-50.0)
    assert b.net_pnl_usd < b.gross_pnl_usd  # costs widen the loss


def test_position_pnl_option():
    pos = _opt(contracts=1, premium_paid=6.50)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=8.00)
    # ($8 - $6.50) * 1 * 100 = $150 gross
    assert b.gross_pnl_usd == pytest.approx(150.0)


def test_position_pnl_no_mark_returns_entry_leg_cost_only():
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=None)
    assert b.gross_pnl_usd == 0.0
    # Still has the entry-leg cost (half of round-trip)
    full_rt = pnl.model_position_cost(pos).round_trip_usd
    assert b.modelled_costs_usd == pytest.approx(full_rt / 2)


def test_portfolio_pnl_aggregates_across_positions():
    portfolio = {
        "positions": [
            _etf(symbol="TQQQ", shares=10, avg_cost=70.0),
            _etf(symbol="SOXL", shares=20, avg_cost=25.0),
            _opt(underlying="SPY", contracts=1, premium_paid=6.50),
        ]
    }
    marks = {
        "TQQQ": 75.0,
        "SOXL": 22.0,
        "SPY|530.0|2026-06-19|call": 8.0,
    }
    b = pnl.compute_portfolio_pnl(portfolio=portfolio, marks=marks)
    # Gross: TQQQ +50, SOXL -60, SPY call +150 = +140
    assert b.gross_pnl_usd == pytest.approx(140.0)
    assert b.modelled_costs_usd > 0
    assert b.net_pnl_usd == pytest.approx(b.gross_pnl_usd - b.modelled_costs_usd)


def test_portfolio_pnl_missing_marks_zero_gross_per_unmarked():
    portfolio = {"positions": [_etf(shares=10, avg_cost=70.0)]}
    b = pnl.compute_portfolio_pnl(portfolio=portfolio, marks=None)
    assert b.gross_pnl_usd == 0.0
    assert b.modelled_costs_usd > 0
