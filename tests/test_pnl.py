"""Trading-cost model + Gross / Net P&L tests.

Costs come from the Alpaca cost model (lib.alpaca_costs): commission-free,
SEC + FINRA TAF sell-side only (rounded up to the cent), and per-side slippage
that is the dominant real friction. These tests verify the per-leg breakdown
and the entry/exit split that keeps the synthetic balance from double-counting.
"""
from __future__ import annotations

import pytest

from lib import alpaca_costs, pnl


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


# ---------- ETF cost components ----------


def test_etf_commission_is_zero():
    """Alpaca retail US ETFs are commission-free, at any size."""
    assert pnl.model_etf_cost(shares=100, price_usd=50.0).commission_usd == 0.0
    assert pnl.model_etf_cost(shares=1, price_usd=5.0).commission_usd == 0.0


def test_etf_half_spread_is_default_slippage():
    """Per-leg slippage = 2 bps of notional for a default (liquid) name."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)  # $5,000 notional
    # 2 bps × $5,000 = $1.00 per side
    assert leg.half_spread_usd == pytest.approx(1.00)


def test_etf_thin_name_uses_override_slippage():
    """A thin ticker (override) costs more per side than the default."""
    liquid = pnl.model_etf_cost(shares=100, price_usd=50.0, symbol="TQQQ")
    thin = pnl.model_etf_cost(shares=100, price_usd=50.0, symbol="HIBL")
    assert thin.half_spread_usd > liquid.half_spread_usd
    # HIBL override is 15 bps/side → $7.50 on $5,000
    assert thin.half_spread_usd == pytest.approx(7.50)


def test_etf_reg_fees_present_on_round_trip():
    """SEC + FINRA TAF apply on the sell side only, charged once per round-trip,
    each rounded up to the cent."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)
    # SEC: ceil_cent($5,000 × 0.0000206 = $0.103) = $0.11
    # TAF: ceil_cent(100 × 0.000195 = $0.0195) = $0.02
    assert leg.reg_fees_usd == pytest.approx(0.13)


def test_etf_round_trip_sums_everything():
    """round_trip = 2×(slippage + commission) + sell-side reg fees."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)
    expected = 2 * (leg.half_spread_usd + leg.commission_usd) + leg.reg_fees_usd
    assert leg.round_trip_usd == pytest.approx(expected)
    # $5k trade: 2×$1.00 slippage + $0 commission + $0.13 reg = $2.13
    assert leg.round_trip_usd == pytest.approx(2.13)


def test_etf_entry_and_exit_legs_split_reg_fees():
    """Entry leg = slippage only; exit leg adds the sell-side reg fees. The two
    sum to the round-trip (no double-count)."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)
    assert leg.entry_leg_usd == pytest.approx(leg.half_spread_usd)
    assert leg.exit_leg_usd == pytest.approx(leg.half_spread_usd + leg.reg_fees_usd)
    assert leg.entry_leg_usd + leg.exit_leg_usd == pytest.approx(leg.round_trip_usd)


def test_etf_finra_taf_capped():
    """FINRA TAF caps per trade even on very large share counts."""
    leg = pnl.model_etf_cost(shares=100_000, price_usd=50.0)
    # SEC: ceil_cent($5M × 0.0000206 = $103.0) = $103.00
    # TAF: min(ceil_cent(100,000 × 0.000195 = $19.50), $9.79) = $9.79 (capped)
    assert leg.reg_fees_usd == pytest.approx(103.0 + alpaca_costs.TAF_CAP_PER_TRADE, abs=0.01)


# ---------- Position-shape adapter ----------


def test_position_cost_etf_round_trip_is_slippage_dominated():
    """ETF position, 10 shares @ $70 = $700 notional. Slippage dominates;
    commission is $0. RT ≈ 2×$0.14 slippage + ~$0.03 reg ≈ $0.31."""
    cost = pnl.model_position_cost(_etf(shares=10, avg_cost=70.0))
    assert cost.commission_usd == 0.0
    assert 0.25 < cost.round_trip_usd < 0.40


# ---------- Position P&L paths ----------


def test_position_pnl_etf_gain():
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=75.0)
    assert b.gross_pnl_usd == pytest.approx(50.0)  # 10 shares × $5
    assert b.modelled_costs_usd > 0
    assert b.net_pnl_usd == pytest.approx(b.gross_pnl_usd - b.modelled_costs_usd)


def test_position_pnl_etf_loss():
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=65.0)
    assert b.gross_pnl_usd == pytest.approx(-50.0)
    assert b.net_pnl_usd < b.gross_pnl_usd  # costs widen the loss


def test_position_pnl_no_mark_returns_entry_leg_cost_only():
    """Entry leg = slippage + commission, no sell-side fees yet."""
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=None)
    assert b.gross_pnl_usd == 0.0
    full = pnl.model_position_cost(pos)
    assert b.modelled_costs_usd == pytest.approx(full.entry_leg_usd)
    # Reg fees specifically NOT counted yet (we haven't sold)
    assert b.modelled_costs_usd < full.round_trip_usd


# ---------- Portfolio aggregation ----------


def test_portfolio_pnl_aggregates_across_positions():
    portfolio = {
        "positions": [
            _etf(symbol="TQQQ", shares=10, avg_cost=70.0),
            _etf(symbol="SOXL", shares=20, avg_cost=25.0),
            _etf(symbol="SQQQ", shares=10, avg_cost=8.0),
        ]
    }
    marks = {
        "TQQQ": 75.0,
        "SOXL": 22.0,
        "SQQQ": 9.0,
    }
    b = pnl.compute_portfolio_pnl(portfolio=portfolio, marks=marks)
    # Gross: TQQQ +50, SOXL -60, SQQQ +10 = 0
    assert b.gross_pnl_usd == pytest.approx(0.0)
    assert b.modelled_costs_usd > 0
    assert b.net_pnl_usd == pytest.approx(b.gross_pnl_usd - b.modelled_costs_usd)


def test_portfolio_pnl_missing_marks_zero_gross_per_unmarked():
    portfolio = {"positions": [_etf(shares=10, avg_cost=70.0)]}
    b = pnl.compute_portfolio_pnl(portfolio=portfolio, marks=None)
    assert b.gross_pnl_usd == 0.0
    assert b.modelled_costs_usd > 0
