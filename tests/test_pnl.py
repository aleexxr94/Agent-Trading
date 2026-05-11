"""Trading-cost model + Gross / Net P&L tests.

Costs calibrated to IBKR Pro retail UK USD. Key tests verify the
**minimum-commission** behaviour which dominates costs on a small
account (the original cost model missed this and understated friction
by ~8× on $2.5k-sized positions).
"""
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


# ---------- ETF cost components ----------


def test_etf_minimum_commission_kicks_in_on_small_position():
    """The $2.5k-account headline: 100 shares × $0.005 = $0.50, but the
    minimum is $1. Per-leg commission must read $1, not $0.50."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)  # $5,000 notional
    # 5 bps half-spread on $5k = $2.50
    assert leg.half_spread_usd == pytest.approx(2.50)
    # 100 × $0.005 = $0.50, but min is $1
    assert leg.commission_usd == pytest.approx(1.00)


def test_etf_per_share_commission_above_minimum():
    """At 1000 shares: 1000 × $0.005 = $5.00, well above the $1 floor."""
    leg = pnl.model_etf_cost(shares=1000, price_usd=50.0)  # $50k notional
    assert leg.commission_usd == pytest.approx(5.00)


def test_etf_max_commission_cap_at_half_percent():
    """0.5% of $200 trade = $1; commission shouldn't exceed that for tiny
    notional with many shares. Tested at $20 share, 100 shares = $2000."""
    # 100 shares × $20 = $2000, per-share comm = $0.50, cap = 0.5% × $2000 = $10
    # raw = $0.50, capped = min($0.50, $10) = $0.50, but min(>=$1) = $1
    # so the floor still wins. Test the cap branch separately:
    # 1,000,000 shares × $0.01 = $10,000 notional. per_share = $5,000.
    # cap = 0.5% of $10k = $50. min/max: max($1, min($5000, $50)) = $50.
    leg = pnl.model_etf_cost(shares=1_000_000, price_usd=0.01)
    assert leg.commission_usd == pytest.approx(50.0)


def test_etf_reg_fees_present_on_round_trip():
    """SEC + FINRA TAF fees only apply on the sell side, charged once per
    round-trip (not doubled)."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)
    # SEC $0.0000278 × $5000 = $0.139
    # FINRA $0.000166 × 100 = $0.0166
    assert leg.reg_fees_usd == pytest.approx(0.139 + 0.0166, abs=0.001)


def test_etf_round_trip_sums_everything():
    """round_trip_usd = 2×(spread + commission) + sell-side reg fees."""
    leg = pnl.model_etf_cost(shares=100, price_usd=50.0)
    expected = 2 * (leg.half_spread_usd + leg.commission_usd) + leg.reg_fees_usd
    assert leg.round_trip_usd == pytest.approx(expected)
    # Sanity: on a $5k trade, round-trip should be ~$7-8 (spread + 2x$1 + fees)
    assert 6.0 < leg.round_trip_usd < 10.0


def test_etf_finra_taf_capped_at_9_90():
    """FINRA TAF caps at $9.90 even on very large trades."""
    # 100,000 shares would otherwise be 100,000 × $0.000166 = $16.60
    leg = pnl.model_etf_cost(shares=100_000, price_usd=50.0)
    # SEC $0.0000278 × $5M = $139 (no cap)
    # FINRA capped at $9.90
    # Verify TAF portion is exactly the cap
    sec_only = 100_000 * 50.0 * pnl.SEC_FEE_PER_USD_SOLD
    assert leg.reg_fees_usd == pytest.approx(sec_only + pnl.FINRA_TAF_MAX_USD, abs=0.01)


# ---------- Options cost components ----------


def test_option_per_contract_commission_includes_occ_fee():
    """IBKR Pro $0.65 + OCC $0.04 = $0.69 per contract per leg."""
    leg = pnl.model_option_cost(contracts=2, premium_usd=650.0)
    # Spread: 25 bps × $1300 = $3.25
    assert leg.half_spread_usd == pytest.approx(3.25)
    # Commission: 2 contracts × ($0.65 + $0.04) = $1.38
    assert leg.commission_usd == pytest.approx(2 * 0.69)


def test_option_sec_fee_on_premium_sold():
    leg = pnl.model_option_cost(contracts=1, premium_usd=650.0)
    # SEC $0.0000278 × $650 (sell-side premium)
    assert leg.reg_fees_usd == pytest.approx(650.0 * pnl.SEC_FEE_PER_USD_SOLD, abs=1e-6)


# ---------- Position-shape adapter ----------


def test_position_cost_etf_round_trip_dominated_by_min_commission():
    """ETF position, 10 shares @ $70 = $700 notional. Min commission $1
    each side = $2, plus $0.70 spread RT, plus tiny reg fees."""
    cost = pnl.model_position_cost(_etf(shares=10, avg_cost=70.0))
    # Total RT: 2 × ($0.35 spread + $1 commission) + reg ≈ $2.70 + ~$0.02
    assert 2.6 < cost.round_trip_usd < 2.9


def test_position_cost_option_uses_dollar_premium():
    cost = pnl.model_position_cost(_opt(contracts=1, premium_paid=6.50))
    # Spread per leg: 25 bps × $650 = $1.625 ; commission: $0.65 + $0.04 = $0.69
    # RT: 2 × ($1.625 + $0.69) + SEC fee on $650 sold
    expected_rt = 2 * (1.625 + 0.69) + (650.0 * pnl.SEC_FEE_PER_USD_SOLD)
    assert cost.round_trip_usd == pytest.approx(expected_rt, abs=0.01)


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


def test_position_pnl_option():
    pos = _opt(contracts=1, premium_paid=6.50)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=8.00)
    # ($8 - $6.50) × 1 × 100 = $150 gross
    assert b.gross_pnl_usd == pytest.approx(150.0)


def test_position_pnl_no_mark_returns_entry_leg_cost_only():
    """Entry leg = half-spread + commission, no sell-side fees yet."""
    pos = _etf(shares=10, avg_cost=70.0)
    b = pnl.compute_position_pnl(position=pos, current_mark_usd=None)
    assert b.gross_pnl_usd == 0.0
    full = pnl.model_position_cost(pos)
    assert b.modelled_costs_usd == pytest.approx(full.half_spread_usd + full.commission_usd)
    # Reg fees specifically NOT counted yet (we haven't sold)
    assert b.modelled_costs_usd < full.round_trip_usd / 2 + 0.02  # close to half-RT


# ---------- Portfolio aggregation ----------


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
    # Gross: TQQQ +50, SOXL -60, SPY +150 = +140
    assert b.gross_pnl_usd == pytest.approx(140.0)
    assert b.modelled_costs_usd > 0
    assert b.net_pnl_usd == pytest.approx(b.gross_pnl_usd - b.modelled_costs_usd)


def test_portfolio_pnl_missing_marks_zero_gross_per_unmarked():
    portfolio = {"positions": [_etf(shares=10, avg_cost=70.0)]}
    b = pnl.compute_portfolio_pnl(portfolio=portfolio, marks=None)
    assert b.gross_pnl_usd == 0.0
    assert b.modelled_costs_usd > 0


# ---------- Headline: real-world honesty check ----------


def test_realistic_small_position_round_trip_is_dominated_by_commission():
    """The $2.5k account headline test. A typical position is $200–300 with
    10–20 shares of a leveraged ETF. Most of the friction is the IBKR
    minimum commission, NOT the spread.
    """
    # 4 shares of TQQQ at $70 = $280 notional
    cost = pnl.model_etf_cost(shares=4, price_usd=70.0)
    # Spread per leg: 5 bps × $280 = $0.14   (round-trip: $0.28)
    # Commission per leg: max($1, 4×$0.005) = $1   (round-trip: $2)
    # Reg fees: ~$0.008 SEC + ~$0.0007 TAF ≈ $0.008
    # Total RT ≈ $0.28 + $2.00 + $0.008 ≈ $2.29
    assert cost.commission_usd == pytest.approx(1.00)
    assert 2.0 < cost.round_trip_usd < 2.5

    # Sanity check: commission alone is ~80 bps of notional round-trip,
    # which is the cost burden that didn't exist in the old model.
    commission_bps_rt = (2 * cost.commission_usd) / cost.notional_usd * 10_000
    assert 60 < commission_bps_rt < 80    # ~71 bps RT just on commission
