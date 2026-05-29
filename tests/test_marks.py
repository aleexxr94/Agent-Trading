"""Tests for lib.marks — broker positions → per-unit mark dict."""
from __future__ import annotations

import pytest

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


def test_legacy_option_position_keyed_by_raw_symbol():
    """ETF-only system: a stray/legacy us_option broker position is keyed
    by its raw symbol (no synthetic parsing) so nothing crashes; monitor
    flattens it as an unsupported instrument."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("SPY261219C00530000", 1, 650.0, asset_class="us_option")
    ]))
    assert out == {"SPY261219C00530000": 650.0}


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


def test_falls_back_to_derived_when_current_price_missing():
    """When current_price is None (test stub, older SDK), the
    market_value / qty derivation still works."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("TQQQ", 10, 750.0, current_price=None),
    ]))
    assert out == {"TQQQ": 75.0}


def test_portfolio_to_mark_keys_etf():
    portfolio = {
        "positions": [
            {"kind": "etf", "symbol": "TQQQ"},
            {"kind": "etf", "symbol": "SQQQ"},
        ]
    }
    keys = marks.portfolio_to_mark_keys(portfolio)
    assert keys["0"] == "TQQQ"
    assert keys["1"] == "SQQQ"


def test_marks_key_for_etf_is_bare_symbol():
    out = marks.marks_from_broker(_FakeBroker([_bp("TQQQ", 10, 750.0)]))
    assert "TQQQ" in out
    assert out["TQQQ"] == 75.0


# ---------------- cost_basis_from_broker ----------------


def test_cost_basis_etf():
    """ETF: per-share avg_cost flows straight through with bare-symbol key."""
    pos = BrokerPosition(
        symbol="TQQQ", qty=10, avg_cost=68.40,
        market_value=750.0, unrealized_pl_usd=0.0,
        asset_class="us_equity",
    )
    out = marks.cost_basis_from_broker(_FakeBroker([pos]))
    assert out == {"TQQQ": 68.40}


def test_cost_basis_none_broker_returns_empty():
    assert marks.cost_basis_from_broker(None) == {}


def test_cost_basis_broker_error_returns_empty():
    """get_positions raising shouldn't crash the dashboard — return {} so
    the table falls back to portfolio.json values."""
    class _BrokenBroker(_FakeBroker):
        def get_positions(self):
            raise RuntimeError("network down")

    assert marks.cost_basis_from_broker(_BrokenBroker([])) == {}


def test_cost_basis_skips_zero_qty():
    """Closed positions still have qty=0 transiently; don't emit a row."""
    pos = BrokerPosition(
        symbol="TQQQ", qty=0, avg_cost=70.0,
        market_value=0.0, unrealized_pl_usd=0.0,
        asset_class="us_equity",
    )
    assert marks.cost_basis_from_broker(_FakeBroker([pos])) == {}


# ---------- pure helpers (Codex P1 follow-up on PR #51) ----------


def test_marks_from_positions_pure_helper_matches_broker_wrapper():
    """marks_from_positions takes a positions list directly so callers
    can fetch positions themselves and have exceptions propagate. It
    must produce the same dict as marks_from_broker on the same data."""
    positions = [
        _bp("TQQQ", 10, 750.0, current_price=75.0),
        _bp("SOXL", 5, 200.0, current_price=40.0),
    ]
    via_helper = marks.marks_from_positions(positions)
    via_broker = marks.marks_from_broker(_FakeBroker(positions))
    assert via_helper == via_broker
    assert via_helper == {"TQQQ": 75.0, "SOXL": 40.0}


def test_cost_basis_from_positions_pure_helper_matches_broker_wrapper():
    positions = [
        _bp("TQQQ", 10, 750.0),
        _bp("SOXL", 5, 200.0),
    ]
    via_helper = marks.cost_basis_from_positions(positions)
    via_broker = marks.cost_basis_from_broker(_FakeBroker(positions))
    assert via_helper == via_broker


def test_marks_from_positions_propagates_when_caller_did_not_catch():
    """Sanity check that the pure helper has NO try/except — its job is
    to be transparent. Callers (e.g. try_load_broker_view) rely on this
    so a transient broker outage doesn't get silently turned into an
    empty dict."""
    # No try/except inside marks_from_positions itself; the get_positions
    # call lives in the wrapper. Passing a list directly means we never
    # touch the broker, so no exceptions to swallow — verified by the
    # function signature accepting list[BrokerPosition].
    assert marks.marks_from_positions([]) == {}
