from __future__ import annotations

import math

import pytest

from lib import options


def test_atm_call_put_parity():
    inp_c = options.BSInputs(spot=100, strike=100, t_years=0.5, sigma=0.20, kind="call")
    inp_p = options.BSInputs(spot=100, strike=100, t_years=0.5, sigma=0.20, kind="put")
    c = options.bs_price(inp_c)
    p = options.bs_price(inp_p)
    # Put-call parity: C - P = S - K e^{-rT}  (q=0)
    expected = 100 - 100 * math.exp(-0.045 * 0.5)
    assert (c - p) == pytest.approx(expected, rel=1e-3)


def test_call_delta_roughly_half_atm():
    g = options.greeks(options.BSInputs(spot=100, strike=100, t_years=0.5, sigma=0.20, kind="call"))
    assert 0.45 < g.delta < 0.65
    assert g.gamma > 0
    assert g.vega > 0
    assert g.theta < 0  # long calls bleed theta


def test_iv_solver_round_trip():
    truth = options.BSInputs(spot=100, strike=100, t_years=0.25, sigma=0.30, kind="call")
    px = options.bs_price(truth)
    iv = options.implied_vol(
        market_price=px, spot=100, strike=100, t_years=0.25, kind="call"
    )
    assert iv == pytest.approx(0.30, abs=1e-3)


def test_iv_below_intrinsic_returns_zero():
    iv = options.implied_vol(market_price=0.001, spot=100, strike=80, t_years=0.5, kind="call")
    assert iv == 0.0  # 0.001 << 20 intrinsic


def test_iv_percentile():
    history = [0.10, 0.15, 0.20, 0.25, 0.30]
    assert options.iv_percentile(0.22, history) == 60.0
    assert options.iv_percentile(0.05, history) == 0.0
    assert options.iv_percentile(0.99, history) == 100.0


def test_chain_liquidity_rejects_wide_spread():
    assert options.passes_chain_liquidity(bid=1.0, ask=1.05, open_interest=500)
    assert not options.passes_chain_liquidity(bid=1.0, ask=1.30, open_interest=500)
    assert not options.passes_chain_liquidity(bid=1.0, ask=1.05, open_interest=10)
    assert not options.passes_chain_liquidity(bid=0, ask=1.05, open_interest=500)
