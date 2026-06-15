"""Alpaca live-cost model tests.

Verifies the reference worked examples (regulatory fees, TAF cap, ceil-to-cent),
the slippage model (default + per-ticker overrides), and that the cost flows
through compute_trades_pnl so realized P&L is net of fees + slippage.
"""
from __future__ import annotations

import pytest

from lib import alpaca_costs, trades


# ---------- ceil-to-cent ----------


def test_ceil_to_cent_rounds_up():
    assert alpaca_costs.ceil_to_cent(0.206) == 0.21
    assert alpaca_costs.ceil_to_cent(0.0975) == 0.10
    assert alpaca_costs.ceil_to_cent(0.01) == 0.01
    assert alpaca_costs.ceil_to_cent(0.0) == 0.0


# ---------- regulatory fees (reference worked examples) ----------


def test_regulatory_sell_fee_tqqq_example():
    """Sell $10,000 TQQQ @ ~$90 (≈111 sh): SEC $0.21 + TAF $0.03 ≈ $0.24."""
    fee = alpaca_costs.regulatory_sell_fee(sell_notional=10_000.0, shares_sold=10_000 / 90)
    assert fee == pytest.approx(0.24, abs=0.005)


def test_regulatory_sell_fee_sqqq_example():
    """Sell $10,000 SQQQ @ ~$20 (500 sh): SEC $0.21 + TAF $0.10 ≈ $0.31."""
    fee = alpaca_costs.regulatory_sell_fee(sell_notional=10_000.0, shares_sold=500)
    assert fee == pytest.approx(0.31, abs=0.005)


def test_regulatory_taf_cap():
    """TAF caps per trade on huge share counts; SEC keeps scaling."""
    fee = alpaca_costs.regulatory_sell_fee(sell_notional=5_000_000.0, shares_sold=100_000)
    sec = alpaca_costs.ceil_to_cent(5_000_000.0 * alpaca_costs.SEC_FEE_PER_DOLLAR)
    assert fee == pytest.approx(sec + alpaca_costs.TAF_CAP_PER_TRADE, abs=0.01)


# ---------- slippage ----------


def test_slippage_default_and_override():
    assert alpaca_costs.slippage_bps_for("TQQQ") == alpaca_costs.SLIPPAGE_BPS_PER_SIDE
    assert alpaca_costs.slippage_bps_for(None) == alpaca_costs.SLIPPAGE_BPS_PER_SIDE
    # Thin name override is wider.
    assert alpaca_costs.slippage_bps_for("HIBL") > alpaca_costs.SLIPPAGE_BPS_PER_SIDE
    # Case-insensitive.
    assert alpaca_costs.slippage_bps_for("hibl") == alpaca_costs.slippage_bps_for("HIBL")


def test_slippage_cost_scales_with_notional():
    cost = alpaca_costs.slippage_cost(symbol="TQQQ", notional=10_000.0)
    assert cost == pytest.approx(10_000.0 * alpaca_costs.SLIPPAGE_BPS_PER_SIDE / 10_000)


# ---------- fill_cost ----------


def test_fill_cost_buy_has_no_regulatory_fee():
    fee, slip = alpaca_costs.fill_cost(side="buy", symbol="TQQQ", shares=10, price=90.0)
    assert fee == 0.0          # commission $0 + no reg on buys
    assert slip > 0.0          # slippage applies to both sides


def test_fill_cost_sell_has_regulatory_fee():
    fee, slip = alpaca_costs.fill_cost(side="sell", symbol="TQQQ", shares=500, price=20.0)
    assert fee == pytest.approx(0.31, abs=0.005)
    assert slip > 0.0


def test_commission_is_zero():
    assert alpaca_costs.COMMISSION_PER_TRADE == 0.0


# ---------- stubs ($0 for cash, long-only) ----------


def test_margin_and_borrow_stubs_are_zero():
    assert alpaca_costs.margin_interest_cost(daily_debit_usd=1_000.0) == 0.0
    assert alpaca_costs.borrow_fee(symbol="SQQQ", notional=1_000.0, days=30) == 0.0


# ---------- integration: realized P&L is net of fees + slippage ----------


def test_model_order_fill_costs_single_sell():
    fills = [{"side": "sell", "symbol": "SQQQ", "shares": 500, "price": 20.0}]
    [(fee, slip)] = alpaca_costs.model_order_fill_costs(fills)
    assert fee == pytest.approx(0.31, abs=0.005)            # SEC + TAF on $10k
    assert slip == pytest.approx(10_000.0 * alpaca_costs.SLIPPAGE_BPS_PER_SIDE / 10_000)


def test_model_order_fill_costs_splits_fee_once_across_partials():
    """Order-level fee computed once, split pro-rata; not re-rounded per fill."""
    fills = [
        {"side": "sell", "symbol": "SQQQ", "shares": 250, "price": 20.0},
        {"side": "sell", "symbol": "SQQQ", "shares": 250, "price": 20.0},
    ]
    res = alpaca_costs.model_order_fill_costs(fills)
    total_fee = sum(f for f, _ in res)
    assert total_fee == pytest.approx(
        alpaca_costs.regulatory_sell_fee(sell_notional=10_000.0, shares_sold=500)
    )


def test_model_order_fill_costs_buy_has_no_fee():
    fills = [{"side": "buy", "symbol": "TQQQ", "shares": 10, "price": 90.0}]
    [(fee, slip)] = alpaca_costs.model_order_fill_costs(fills)
    assert fee == 0.0
    assert slip > 0.0


def test_compute_trades_pnl_nets_fees_and_slippage():
    """A closed round-trip: net P&L = gross − fees − slippage. The regression
    that proves paper Sharpe is no longer gross."""
    rows = [
        {
            "activity_id": "b1", "symbol": "TQQQ", "kind": "etf", "side": "buy",
            "qty": 10, "fill_price": 50.0, "fees_usd": 0.0, "slippage_usd": 0.10,
            "filled_at": "2026-06-01T13:00:00Z", "run_id": None,
        },
        {
            "activity_id": "s1", "symbol": "TQQQ", "kind": "etf", "side": "sell",
            "qty": 10, "fill_price": 55.0, "fees_usd": 0.12, "slippage_usd": 0.11,
            "filled_at": "2026-06-02T13:00:00Z", "run_id": None,
        },
    ]
    pnl = trades.compute_trades_pnl(rows, costs=[], marks={})
    assert len(pnl.closed) == 1
    c = pnl.closed[0]
    assert c.gross_pnl_usd == pytest.approx(50.0)           # (55-50)*10
    assert c.fees_usd == pytest.approx(0.12)                # sell-side reg
    assert c.slippage_usd == pytest.approx(0.21)            # 0.10 buy + 0.11 sell
    assert c.net_pnl_usd == pytest.approx(50.0 - 0.12 - 0.21)
    # Net is strictly below gross — the whole point.
    assert pnl.total_realised_net_usd < pnl.total_realised_gross_usd
    assert pnl.total_realised_slippage_usd == pytest.approx(0.21)
