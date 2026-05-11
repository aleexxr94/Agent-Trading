"""Tests for lib.orders — diff target portfolio vs broker positions."""
from __future__ import annotations

import pytest

from lib import orders
from lib.broker import BrokerPosition


def _bp(symbol, qty, asset_class="us_equity") -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol, qty=qty, avg_cost=50.0,
        market_value=qty * 50.0, unrealized_pl_usd=0.0,
        asset_class=asset_class,
    )


def _etf(symbol, shares, **over):
    base = {
        "kind": "etf", "symbol": symbol, "shares": shares, "avg_cost": 50.0,
        "leverage_factor": 3.0, "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
    }
    base.update(over)
    return base


def _option(underlying, **over):
    base = {
        "kind": "option", "underlying": underlying, "type": "call",
        "strike": 530.0, "expiry": "2026-06-19", "dte": 40,
        "contracts": 1, "premium_paid": 6.50,
        "greeks": {"delta": 0.45, "gamma": 0.02, "theta": -0.04, "vega": 0.18,
                    "iv": 0.18, "iv_percentile": 35},
        "entry_thesis": "x", "kill_conditions": {"max_loss_pct": 100},
        "position_pct": 5.0,
    }
    base.update(over)
    return base


# ---------- is_enabled ----------


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv("ORDERS_ENABLED", raising=False)
    assert orders.is_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("false", False), ("False", False), ("0", False),
    ("true", True), ("True", True), ("TRUE", True),
    ("yes", False),  # only literal 'true' counts
])
def test_is_enabled_env_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("ORDERS_ENABLED", val)
    assert orders.is_enabled() is expected


# ---------- diff_portfolio (ETF cases) ----------


def test_diff_empty_target_empty_current():
    plan = orders.diff_portfolio({"positions": []}, [])
    assert plan.total_legs == 0
    assert plan.skipped == []


def test_diff_fresh_open_when_no_current():
    plan = orders.diff_portfolio({"positions": [_etf("TQQQ", 4)]}, [])
    assert len(plan.requests) == 1
    req = plan.requests[0]
    assert req.symbol == "TQQQ"
    assert req.qty == 4 and req.side == "buy"
    assert plan.closes == []


def test_diff_full_close_when_held_but_not_in_target():
    """ETF held but not in target portfolio → full sell."""
    plan = orders.diff_portfolio({"positions": []}, [_bp("TQQQ", 10)])
    assert len(plan.closes) == 1 and len(plan.requests) == 0
    cl = plan.closes[0]
    assert cl.symbol == "TQQQ" and cl.qty == 10 and cl.side == "sell"


def test_diff_trim_to_target():
    """Holding 10 shares, target wants 4 → sell 6."""
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4)]},
        [_bp("TQQQ", 10)],
    )
    assert len(plan.requests) == 1
    req = plan.requests[0]
    assert req.symbol == "TQQQ" and req.qty == 6 and req.side == "sell"
    assert plan.closes == []


def test_diff_add_to_existing():
    """Holding 4 shares, target wants 10 → buy 6."""
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 10)]},
        [_bp("TQQQ", 4)],
    )
    assert len(plan.requests) == 1
    req = plan.requests[0]
    assert req.symbol == "TQQQ" and req.qty == 6 and req.side == "buy"


def test_diff_no_change_when_target_matches_current():
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4)]},
        [_bp("TQQQ", 4)],
    )
    assert plan.total_legs == 0


def test_diff_multi_symbol_mix():
    target = {"positions": [_etf("TQQQ", 4), _etf("SOXL", 10)]}
    current = [_bp("TQQQ", 10), _bp("FAS", 5)]
    plan = orders.diff_portfolio(target, current)
    by_sym = {r.symbol: r for r in plan.requests + plan.closes}
    assert by_sym["TQQQ"].side == "sell" and by_sym["TQQQ"].qty == 6
    assert by_sym["SOXL"].side == "buy"  and by_sym["SOXL"].qty == 10
    assert by_sym["FAS"].side  == "sell" and by_sym["FAS"].qty  == 5


# ---------- OSI symbol builder ----------


def test_osi_symbol_spy_call():
    assert orders.osi_symbol(
        underlying="SPY", expiry="2026-06-19", type="call", strike=530.0
    ) == "SPY260619C00530000"


def test_osi_symbol_qqq_put():
    assert orders.osi_symbol(
        underlying="QQQ", expiry="2026-06-05", type="put", strike=440.0
    ) == "QQQ260605P00440000"


def test_osi_symbol_fractional_strike():
    """Half-strikes encode correctly: 437.5 → 00437500."""
    assert orders.osi_symbol(
        underlying="IWM", expiry="2026-12-19", type="call", strike=437.5
    ) == "IWM261219C00437500"


@pytest.mark.parametrize("bad_type", ["put_spread", "iron_condor", "", "C"])
def test_osi_symbol_rejects_bad_type(bad_type):
    with pytest.raises(ValueError):
        orders.osi_symbol(underlying="SPY", expiry="2026-06-19", type=bad_type, strike=530.0)


def test_osi_symbol_rejects_invalid_expiry_format():
    with pytest.raises(ValueError):
        orders.osi_symbol(underlying="SPY", expiry="19-06-2026", type="call", strike=530.0)


# ---------- diff_portfolio (option handling) ----------


def _bpo(osi, qty=1) -> BrokerPosition:
    return BrokerPosition(
        symbol=osi, qty=qty, avg_cost=6.50,
        market_value=qty * 650.0, unrealized_pl_usd=0.0,
        asset_class="us_option",
    )


def test_option_fresh_open():
    """Target has 1 SPY call, broker has nothing → buy 1 contract."""
    plan = orders.diff_portfolio({"positions": [_option("SPY")]}, [])
    assert len(plan.requests) == 1
    req = plan.requests[0]
    assert req.symbol == "SPY260619C00530000"
    assert req.qty == 1 and req.side == "buy"
    assert plan.skipped == []


def test_option_full_close_when_held_but_not_in_target():
    """Holding 2 contracts, target wants none → sell 2."""
    osi = "SPY260619C00530000"
    plan = orders.diff_portfolio({"positions": []}, [_bpo(osi, qty=2)])
    assert plan.closes[0].symbol == osi
    assert plan.closes[0].qty == 2 and plan.closes[0].side == "sell"


def test_option_no_change_when_matches():
    osi = "SPY260619C00530000"
    plan = orders.diff_portfolio(
        {"positions": [_option("SPY", contracts=1)]},
        [_bpo(osi, qty=1)],
    )
    assert plan.total_legs == 0


def test_option_buy_more_when_under_target():
    osi = "SPY260619C00530000"
    plan = orders.diff_portfolio(
        {"positions": [_option("SPY", contracts=3)]},
        [_bpo(osi, qty=1)],
    )
    assert len(plan.requests) == 1
    assert plan.requests[0].symbol == osi
    assert plan.requests[0].qty == 2 and plan.requests[0].side == "buy"


def test_option_trim_when_over_target():
    osi = "SPY260619C00530000"
    plan = orders.diff_portfolio(
        {"positions": [_option("SPY", contracts=1)]},
        [_bpo(osi, qty=3)],
    )
    assert len(plan.requests) == 1
    assert plan.requests[0].qty == 2 and plan.requests[0].side == "sell"


def test_mixed_etf_and_option_plan():
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4), _option("SPY"), _option("QQQ", type="put", strike=440.0, expiry="2026-06-05")]},
        [],
    )
    symbols = {r.symbol for r in plan.requests}
    assert "TQQQ" in symbols
    assert "SPY260619C00530000" in symbols
    assert "QQQ260605P00440000" in symbols
    assert len(plan.requests) == 3


def test_malformed_option_target_surfaces_as_skipped():
    """Missing 'strike' field → can't build OSI → surfaced as skipped."""
    bad = {"kind": "option", "underlying": "SPY", "type": "call",
            "expiry": "2026-06-19", "contracts": 1, "premium_paid": 6.50,
            "greeks": {}, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 100}, "position_pct": 5.0}
    plan = orders.diff_portfolio({"positions": [bad]}, [])
    assert len(plan.skipped) == 1
    assert "OSI" in plan.skipped[0]["reason"]
    assert plan.requests == []


# ---------- submit_plan ----------


class _FakeBroker:
    def __init__(self, fail_symbols=()):
        self.submitted: list = []
        self.fail_symbols = set(fail_symbols)

    @property
    def name(self): return "fake"

    def get_account(self): raise NotImplementedError

    def get_positions(self): return []

    def submit_order(self, req):
        from lib.broker import OrderResult
        self.submitted.append(req)
        if req.symbol in self.fail_symbols:
            raise RuntimeError("simulated broker 500")
        return OrderResult(
            broker_order_id=f"id-{req.symbol}", symbol=req.symbol,
            qty=req.qty, side=req.side,
            submitted_at="2026-05-11T08:00:00Z", status="accepted",
        )

    def cancel_all(self): return 0

    def flatten(self, sym): return None


def test_submit_plan_executes_closes_then_opens():
    """Closes go first so cash frees up before opens."""
    plan = orders.diff_portfolio(
        {"positions": [_etf("SOXL", 10)]},
        [_bp("FAS", 5)],
    )
    broker = _FakeBroker()
    results = orders.submit_plan(plan, broker=broker)
    assert len(results) == 2
    # FAS close fires before SOXL open
    assert broker.submitted[0].symbol == "FAS" and broker.submitted[0].side == "sell"
    assert broker.submitted[1].symbol == "SOXL" and broker.submitted[1].side == "buy"
    assert all(r.status == "accepted" for r in results)


def test_submit_plan_isolates_per_order_failures():
    """One bad order doesn't block the rest of the plan."""
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4), _etf("SOXL", 10)]},
        [],
    )
    broker = _FakeBroker(fail_symbols=("TQQQ",))
    results = orders.submit_plan(plan, broker=broker)
    assert len(results) == 2
    statuses = {r.symbol: r.status for r in results}
    assert statuses["TQQQ"].startswith("error:")
    assert statuses["SOXL"] == "accepted"


# ---------- Alpaca structured error parsing ----------


class _AlpacaAPIError(Exception):
    """Stand-in for alpaca-py's APIError shape (.code + .message)."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def test_alpaca_error_label_prefers_code_and_message():
    e = _AlpacaAPIError(40010001, "time_in_force must be valid")
    assert orders._alpaca_error_label(e) == "error[40010001]: time_in_force must be valid"


def test_alpaca_error_label_falls_back_with_message_only():
    class _PartialError(Exception):
        message = "subscription does not permit querying recent SIP data"
    label = orders._alpaca_error_label(_PartialError())
    assert "_PartialError" in label
    assert "subscription does not permit" in label


def test_alpaca_error_label_falls_back_with_neither():
    label = orders._alpaca_error_label(RuntimeError("simulated 500"))
    assert label == "error: RuntimeError: simulated 500"


def test_submit_plan_renders_structured_alpaca_error():
    """End-to-end: an APIError-shaped exception lands as 'error[code]: msg'
    in the OrderResult.status, not a generic 'error: TypeError: ...'."""
    class _BrokerThatRejects:
        @property
        def name(self): return "fake"
        def get_account(self): raise NotImplementedError
        def get_positions(self): return []
        def submit_order(self, req):
            raise _AlpacaAPIError(40010001, "qty must be an integer")
        def cancel_all(self): return 0
        def flatten(self, sym): return None

    plan = orders.diff_portfolio({"positions": [_etf("TQQQ", 4)]}, [])
    results = orders.submit_plan(plan, broker=_BrokerThatRejects())
    assert len(results) == 1
    assert results[0].status == "error[40010001]: qty must be an integer"
