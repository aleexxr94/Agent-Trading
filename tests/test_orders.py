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
    """A legacy option-shaped position dict — used only to verify the order
    layer DEFENSIVELY REJECTS it (the system is ETF-only)."""
    base = {
        "kind": "option", "underlying": underlying, "type": "call",
        "strike": 530.0, "expiry": "2026-06-19", "dte": 40,
        "contracts": 1, "premium_paid": 6.50,
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


# ---------- diff_portfolio defensively rejects option payloads ----------


def test_option_target_is_rejected_not_traded():
    """ETF-only: an option-shaped target position is dropped into `skipped`
    and NEVER turned into an order."""
    plan = orders.diff_portfolio({"positions": [_option("SPY")]}, [])
    assert plan.requests == []
    assert plan.closes == []
    assert len(plan.skipped) == 1
    assert "option" in plan.skipped[0]["reason"].lower()


def test_osi_symbol_target_is_rejected():
    """A position whose symbol is OSI-shaped is rejected even if kind=etf."""
    bad = _etf("SPY260619C00530000", 1)
    plan = orders.diff_portfolio({"positions": [bad]}, [])
    assert plan.requests == []
    assert len(plan.skipped) == 1


def test_option_only_fields_trigger_rejection():
    """Any option-only field (strike/expiry/contracts/premium_paid/greeks)
    on an otherwise ETF-looking row triggers the defensive reject."""
    bad = _etf("TQQQ", 4)
    bad["strike"] = 100.0
    plan = orders.diff_portfolio({"positions": [bad]}, [])
    assert plan.requests == []
    assert len(plan.skipped) == 1


def test_etf_targets_unaffected_by_reject_path():
    """A clean ETF target still produces a normal order alongside a rejected
    option sibling."""
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4), _option("SPY")]}, [],
    )
    assert len(plan.requests) == 1
    assert plan.requests[0].symbol == "TQQQ"
    assert len(plan.skipped) == 1


# ---------- diff_portfolio hard universe guard ----------


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "TSLA", "TQQ", "AAPL"])
def test_diff_non_universe_target_is_skipped(symbol):
    """Fail-closed: a non-universe symbol (plain index ETF, single name, or a
    typo) is dropped into `skipped`, never planned as an open."""
    plan = orders.diff_portfolio({"positions": [_etf(symbol, 5)]}, [])
    assert plan.requests == []
    assert len(plan.skipped) == 1
    assert plan.skipped[0]["symbol"] == symbol
    assert "universe" in plan.skipped[0]["reason"].lower()


def test_diff_universe_targets_still_planned():
    """Regression: valid universe ETFs still produce the expected opens."""
    plan = orders.diff_portfolio(
        {"positions": [_etf("TQQQ", 4), _etf("SOXL", 3), _etf("BITX", 2)]}, [],
    )
    assert {r.symbol for r in plan.requests} == {"TQQQ", "SOXL", "BITX"}
    assert plan.skipped == []


def test_in_universe_helper():
    assert orders.in_universe("TQQQ") is True
    assert orders.in_universe("SPY") is False


# ---------- submit_plan hard universe guard ----------


def test_submit_plan_refuses_non_universe_buy():
    """A non-universe buy is refused at the broker boundary — broker.submit_order
    is never called."""
    from lib.broker import OrderRequest
    plan = orders.OrderPlan(
        requests=[OrderRequest(symbol="SPY", qty=5, side="buy", order_type="market")],
    )
    broker = _FakeBroker()
    results = orders.submit_plan(plan, broker=broker)
    assert len(results) == 1
    assert results[0].status.startswith("skipped: symbol not in ETF-only universe")
    assert broker.submitted == [], "no non-universe order should reach the broker"


def test_submit_plan_refuses_non_universe_sell_close():
    """Owner decision (block-everything): even a close/sell of a non-universe
    symbol is refused here — stray-holding exits are handled by monitor flatten
    or a manual action, not this order path."""
    from lib.broker import OrderRequest
    plan = orders.OrderPlan(
        closes=[OrderRequest(symbol="AAPL", qty=10, side="sell", order_type="market")],
    )
    broker = _FakeBroker()
    results = orders.submit_plan(plan, broker=broker)
    assert len(results) == 1
    assert results[0].status.startswith("skipped: symbol not in ETF-only universe")
    assert broker.submitted == []


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


# ---------- OSI symbol detector ----------


@pytest.mark.parametrize("symbol", [
    "SPY260619C00530000",   # SPY call
    "TLT260619P00088000",   # TLT put (the May 12 2026 broken pick)
    "QQQ260918P00400500",   # half-dollar strike
    "A260619C00050000",     # 1-char underlying
    "GOOGL260619C00150000", # 5-char underlying
])
def test_is_osi_symbol_recognizes_real_osis(symbol):
    assert orders.is_osi_symbol(symbol)


@pytest.mark.parametrize("symbol", [
    "SPY",                 # plain ETF
    "TQQQ",
    "SPY26C00530000",      # missing date digits
    "SPY260619X00530000",  # invalid C/P slot
    "spy260619c00530000",  # lowercase
    "",
])
def test_is_osi_symbol_rejects_non_osi(symbol):
    assert not orders.is_osi_symbol(symbol)


# ---------- submit_plan defensive OSI rejection ----------


def test_submit_plan_refuses_osi_symbol_order():
    """Fail-closed: if an OSI-shaped order somehow reaches submit_plan it is
    refused outright and never sent to the broker (the system is ETF-only and
    never builds option symbols, so this is a belt-and-braces guard)."""
    from lib.broker import OrderRequest
    osi = "TLT260619P00088000"
    plan = orders.OrderPlan(
        requests=[OrderRequest(symbol=osi, qty=1, side="buy", order_type="market")],
    )
    broker = _FakeBroker()
    results = orders.submit_plan(plan, broker=broker)
    assert len(results) == 1
    assert results[0].symbol == osi
    assert results[0].status.startswith("skipped: option symbols are not supported")
    assert broker.submitted == [], "no OSI order should reach the broker"


def test_submit_plan_does_not_gate_etf_orders():
    """ETF symbols aren't OSI-shaped, so they flow straight through."""
    plan = orders.diff_portfolio({"positions": [_etf("TQQQ", 4)]}, [])
    broker = _FakeBroker()
    results = orders.submit_plan(plan, broker=broker)
    assert len(broker.submitted) == 1
    assert results[0].status == "accepted"


# ---------- _plan_for_symbol: no-cross-zero invariant (v2) ----------


def test_plan_for_symbol_zero_to_long():
    """0 → long N: single buy of N."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=0, target_qty=5)
    assert closes == []
    assert len(opens) == 1
    assert opens[0].side == "buy" and opens[0].qty == 5


def test_plan_for_symbol_long_to_zero_is_pure_close():
    """long N → 0: single sell of N, in the closes list (submitted
    before opens to free cash)."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=4, target_qty=0)
    assert opens == []
    assert len(closes) == 1
    assert closes[0].side == "sell" and closes[0].qty == 4


def test_plan_for_symbol_long_to_larger_long_is_single_buy():
    """long 2 → long 5: single buy of 3 (delta), no close needed."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=2, target_qty=5)
    assert closes == []
    assert len(opens) == 1
    assert opens[0].side == "buy" and opens[0].qty == 3


def test_plan_for_symbol_long_to_smaller_long_is_single_sell():
    """long 5 → long 2: single sell of 3 (delta), no close needed."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=5, target_qty=2)
    assert closes == []
    assert len(opens) == 1
    assert opens[0].side == "sell" and opens[0].qty == 3


def test_plan_for_symbol_short_to_more_short_is_single_sell():
    """Codex P1 regression: short -2 → short -5 should sell 3 (grow
    the short). An earlier same-sign branch inverted this and emitted
    a buy, moving AWAY from the target. The fix collapsed the side
    logic to the simple rule `buy if delta > 0 else sell`.
    """
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=-2, target_qty=-5)
    assert closes == []
    assert len(opens) == 1
    assert opens[0].side == "sell" and opens[0].qty == 3


def test_plan_for_symbol_short_to_less_short_is_single_buy():
    """Codex P1 regression: short -5 → short -2 should buy 3 (cover
    toward zero). An earlier same-sign branch inverted this and
    emitted a sell.
    """
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=-5, target_qty=-2)
    assert closes == []
    assert len(opens) == 1
    assert opens[0].side == "buy" and opens[0].qty == 3


def test_plan_for_symbol_no_change_emits_no_orders():
    """current == target: zero orders."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=3, target_qty=3)
    assert closes == []
    assert opens == []
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=0, target_qty=0)
    assert closes == []
    assert opens == []


def test_plan_for_symbol_long_to_short_splits_into_close_then_open():
    """long N → short M: MUST split into close-N then open-M, never a
    single sell of N+M that crosses zero in one ticket.

    For v2's long-only schema this never triggers, but the invariant
    is defensive. Don't remove this test even if the schema stays
    long-only forever — its absence would make a future short-enabling
    change silently break the no-cross-zero rail.
    """
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=4, target_qty=-3)
    assert len(closes) == 1
    assert closes[0].side == "sell" and closes[0].qty == 4
    assert len(opens) == 1
    assert opens[0].side == "sell" and opens[0].qty == 3


def test_plan_for_symbol_short_to_long_splits_into_close_then_open():
    """short N → long M: split into buy-to-cover-N then open-buy-M."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=-4, target_qty=3)
    assert len(closes) == 1
    assert closes[0].side == "buy" and closes[0].qty == 4
    assert len(opens) == 1
    assert opens[0].side == "buy" and opens[0].qty == 3


def test_plan_for_symbol_short_to_zero_is_buy_to_cover():
    """short N → 0: single buy to cover, in the closes list."""
    closes, opens = orders._plan_for_symbol(symbol="TQQQ", current_qty=-4, target_qty=0)
    assert opens == []
    assert len(closes) == 1
    assert closes[0].side == "buy" and closes[0].qty == 4


def test_diff_portfolio_close_long_then_open_different_etf_uses_separate_orders():
    """The plan-for-symbol invariant is per-symbol, so two unrelated
    long positions (close TQQQ, open SOXL) end up as two independent
    orders in the right buckets (close in `closes`, open in
    `requests`)."""
    from lib.broker import BrokerPosition
    target = {
        "positions": [{
            "kind": "etf", "symbol": "SOXL", "shares": 3,
            "avg_cost": 25.0, "leverage_factor": 3.0,
            "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25},
            "position_pct": 5.0,
        }],
        "all_cash": False,
    }
    current = [BrokerPosition(
        symbol="TQQQ", qty=4.0, avg_cost=70.0,
        market_value=280.0, unrealized_pl_usd=0.0,
        asset_class="us_equity",
    )]
    plan = orders.diff_portfolio(target, current)
    # One close (TQQQ sell 4) + one open (SOXL buy 3).
    assert len(plan.closes) == 1
    assert plan.closes[0].symbol == "TQQQ"
    assert plan.closes[0].side == "sell" and plan.closes[0].qty == 4
    assert len(plan.requests) == 1
    assert plan.requests[0].symbol == "SOXL"
    assert plan.requests[0].side == "buy" and plan.requests[0].qty == 3
