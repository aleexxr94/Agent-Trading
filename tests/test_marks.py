"""Tests for lib.marks — broker positions → per-unit mark dict."""
from __future__ import annotations

from lib import marks
from lib.broker import BrokerPosition


def _bp(symbol, qty, market_value, asset_class="us_equity", current_price=None) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        qty=qty,
        avg_cost=market_value / qty if qty else 0,
        market_value=market_value,
        unrealized_pl_usd=0.0,
        asset_class=asset_class,
        current_price=current_price,
    )


class _FakeBroker:
    def __init__(self, positions):
        self._positions = positions

    @property
    def name(self):
        return "fake"

    def get_account(self):
        raise NotImplementedError

    def get_positions(self):
        return self._positions

    def submit_order(self, *a, **kw):
        raise NotImplementedError

    def cancel_all(self):
        return 0

    def flatten(self, symbol):
        return None


def test_marks_none_broker_returns_empty():
    assert marks.marks_from_broker(None) == {}


def test_marks_empty_positions_returns_empty():
    assert marks.marks_from_broker(_FakeBroker([])) == {}


def test_etf_per_share_mark():
    """ETF: market_value / qty = per-share price."""
    out = marks.marks_from_broker(_FakeBroker([_bp("TQQQ", 10, 750.0)]))
    assert out == {"TQQQ": 75.0}


def test_multiple_etfs():
    bp = [_bp("TQQQ", 10, 750.0), _bp("SOXL", 20, 400.0)]
    out = marks.marks_from_broker(_FakeBroker(bp))
    assert out == {"TQQQ": 75.0, "SOXL": 20.0}


def test_option_per_share_premium_strips_100x_multiplier():
    """Alpaca option market_value already includes the 100x multiplier;
    pnl.compute_position_pnl expects per-share premium, so divide back out."""
    # 1 contract, market_value = $650 = $6.50/share × 100
    out = marks.marks_from_broker(_FakeBroker([
        _bp("SPY261219C00530000", 1, 650.0, asset_class="us_option")
    ]))
    assert out == {"SPY261219C00530000": 6.5}


def test_zero_qty_position_skipped():
    out = marks.marks_from_broker(_FakeBroker([
        _bp("TQQQ", 0, 0.0),
        _bp("SOXL", 5, 100.0),
    ]))
    assert "TQQQ" not in out
    assert out["SOXL"] == 20.0


def test_broker_error_returns_empty_not_crash():
    class _BrokenBroker(_FakeBroker):
        def get_positions(self):
            raise RuntimeError("network down")
    assert marks.marks_from_broker(_BrokenBroker([])) == {}


def test_current_price_preferred_over_derived():
    """When Alpaca reports current_price, use it directly — bypasses the
    market_value / qty derivation which would lose precision on options
    with the 100x multiplier."""
    # If we DERIVE: 4 × 80.50 = 322.00, then 322/4 = 80.5. But say market_value
    # comes in as 322.01 (rounding). Derived = 80.5025 — slightly off.
    # current_price=80.55 says "the live quote is 80.55", and that's what we use.
    out = marks.marks_from_broker(_FakeBroker([
        _bp("TQQQ", 4, 322.01, current_price=80.55),
    ]))
    assert out == {"TQQQ": 80.55}


def test_current_price_preferred_for_options_no_100x_division():
    """For options, current_price is already per-share premium — no /100
    needed. The market_value-derived path would divide; current_price
    path must not."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("SPY261219C00530000", 1, 650.0, asset_class="us_option",
            current_price=6.50),
    ]))
    assert out == {"SPY261219C00530000": 6.50}


def test_falls_back_to_derived_when_current_price_missing():
    """When current_price is None (test stub, older SDK), the existing
    market_value / qty / (100 if option) derivation still works."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("TQQQ", 10, 750.0, current_price=None),
    ]))
    assert out == {"TQQQ": 75.0}


def test_portfolio_to_mark_keys_etf_and_option():
    portfolio = {
        "positions": [
            {"kind": "etf", "symbol": "TQQQ"},
            {"kind": "option", "underlying": "SPY", "type": "call",
             "strike": 530.0, "expiry": "2026-06-19"},
        ]
    }
    keys = marks.portfolio_to_mark_keys(portfolio)
    assert keys["0"] == "TQQQ"
    assert keys["1"] == "SPY|530.0|2026-06-19|call"
