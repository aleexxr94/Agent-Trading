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


# ---------- diff_portfolio (option handling) ----------


def test_options_in_target_are_skipped_with_reason():
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4), _option("SPY")]},
        [],
    )
    # ETF still placed
    assert len(plan.requests) == 1 and plan.requests[0].symbol == "TQQQ"
    # Option surfaced as skipped
    assert len(plan.skipped) == 1
    sk = plan.skipped[0]
    assert sk["underlying"] == "SPY"
    assert "Phase 10d" in sk["reason"]


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
