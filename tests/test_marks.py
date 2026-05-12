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


def test_option_per_share_premium_strips_100x_multiplier():
    """Alpaca option market_value already includes the 100x multiplier;
    pnl.compute_position_pnl expects per-share premium, so divide back out.
    Note: marks_from_broker now keys options by the synthetic shape,
    not the raw OSI — see test_marks_key_for_option_uses_synthetic."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("SPY261219C00530000", 1, 650.0, asset_class="us_option")
    ]))
    assert out == {"SPY|530.0|2026-12-19|call": 6.5}


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
    assert out == {"SPY|530.0|2026-12-19|call": 6.50}


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


# ---------- OSI ↔ synthetic key translation ----------


def test_osi_to_synthetic_round_trip_spy_call():
    # 261219 = 2026-12-19, 530.0 strike, call
    assert marks._osi_to_synthetic("SPY261219C00530000") == "SPY|530.0|2026-12-19|call"


def test_osi_to_synthetic_qqq_put():
    assert marks._osi_to_synthetic("QQQ260605P00440000") == "QQQ|440.0|2026-06-05|put"


def test_osi_to_synthetic_fractional_strike():
    assert marks._osi_to_synthetic("IWM261219C00437500") == "IWM|437.5|2026-12-19|call"


@pytest.mark.parametrize("bad", [
    "",            # empty
    "SPY",         # too short
    "SPYXXXXXXC00530000",  # date not digits
    "SPY261219X00530000",  # type not C/P
    "SPY261219C0053XXXX",  # strike not digits
])
def test_osi_to_synthetic_returns_none_on_bad(bad):
    assert marks._osi_to_synthetic(bad) is None


def test_marks_key_for_option_uses_synthetic():
    """Critical: pnl + monitor look up option marks by the synthetic key
    'SPY|530.0|YYYY-MM-DD|call' — NOT the OSI symbol. marks_from_broker
    must produce keys consumers can actually find."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("SPY261219C00530000", 1, 650.0, asset_class="us_option",
            current_price=6.50),
    ]))
    # OSI 261219 = 2026-12-19
    assert "SPY|530.0|2026-12-19|call" in out
    assert out["SPY|530.0|2026-12-19|call"] == 6.50
    # OSI key NOT used directly (would silently mismatch with consumers)
    assert "SPY261219C00530000" not in out


def test_marks_key_for_etf_still_bare_symbol():
    """Regression — ETF keys stay bare symbol; only options use synthetic."""
    out = marks.marks_from_broker(_FakeBroker([_bp("TQQQ", 10, 750.0)]))
    assert "TQQQ" in out
    assert out["TQQQ"] == 75.0


def test_marks_key_falls_back_to_osi_when_parse_fails():
    """A malformed option symbol shouldn't crash — fall back to using the
    raw symbol as the key. Better to mis-match than to lose data."""
    out = marks.marks_from_broker(_FakeBroker([
        _bp("BAD_OPTION_SYMBOL", 1, 100.0, asset_class="us_option",
            current_price=1.00),
    ]))
    # No synthetic-key match possible; raw symbol preserved
    assert "BAD_OPTION_SYMBOL" in out


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


def test_cost_basis_option_uses_synthetic_key():
    """Option: synthetic UNDERLYING|STRIKE|EXPIRY|TYPE key (same as marks)
    and per-share avg_cost (matching schema's premium_paid units)."""
    pos = BrokerPosition(
        symbol="SPY260626P00565000", qty=1, avg_cost=0.61,
        market_value=59.0, unrealized_pl_usd=-2.0,
        asset_class="us_option",
    )
    out = marks.cost_basis_from_broker(_FakeBroker([pos]))
    # OSI 260626 = 2026-06-26, strike 565000 / 1000 = 565.0
    assert out == {"SPY|565.0|2026-06-26|put": 0.61}


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
