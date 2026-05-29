"""Per-trade PnL accounting tests.

Covers lib.state's append_trade / read_trades / read_trade_activity_ids
plus lib.trades's FIFO matcher and equal-split LLM cost attribution.
Methodology locked: per-position equal split for token cost; real fees
from Alpaca fills (sums per-fill fees_usd into closed-trade total).
"""
from __future__ import annotations

import pytest

from lib import state, trades


# ---------- state.append_trade / read_trades ----------


def _trade(**over) -> dict:
    base = dict(
        activity_id="act-1",
        alpaca_order_id="ord-1",
        symbol="TQQQ",
        kind="etf",
        side="buy",
        qty=10,
        fill_price=70.0,
        fees_usd=0.0,
        filled_at="2026-05-12T14:30:00Z",
        run_id="run-A",
    )
    base.update(over)
    return base


def test_append_trade_writes_and_reads_back(tmp_state):
    state.append_trade(_trade(activity_id="a1"))
    state.append_trade(_trade(activity_id="a2", side="sell", fill_price=75.0))
    rows = state.read_trades()
    assert [r["activity_id"] for r in rows] == ["a1", "a2"]
    assert rows[0]["side"] == "buy" and rows[1]["side"] == "sell"


def test_append_trade_rejects_missing_keys(tmp_state):
    bad = _trade()
    del bad["fill_price"]
    with pytest.raises(ValueError) as ei:
        state.append_trade(bad)
    assert "fill_price" in str(ei.value)


def test_append_trade_allows_missing_run_id(tmp_state):
    """Manual operator fills won't have a run_id — they default to None
    so the attribution layer can decide what to do with them."""
    t = _trade(activity_id="manual-1")
    del t["run_id"]
    state.append_trade(t)
    rows = state.read_trades()
    assert rows[0]["run_id"] is None


def test_read_trade_activity_ids_returns_set(tmp_state):
    state.append_trade(_trade(activity_id="a1"))
    state.append_trade(_trade(activity_id="a2"))
    ids = state.read_trade_activity_ids()
    assert ids == {"a1", "a2"}


def test_read_trades_empty_when_no_file(tmp_state):
    assert state.read_trades() == []
    assert state.read_trade_activity_ids() == set()


# ---------- positions_opened_per_run ----------


def test_positions_opened_counts_distinct_symbols_per_run():
    rows = [
        _trade(activity_id="a", run_id="r1", symbol="TQQQ", side="buy"),
        # Same symbol same run = still ONE position even if partial fills
        _trade(activity_id="b", run_id="r1", symbol="TQQQ", side="buy", qty=5),
        _trade(activity_id="c", run_id="r1", symbol="SOXL", side="buy"),
        _trade(activity_id="d", run_id="r2", symbol="SPY", side="buy"),
        # Sells don't open positions
        _trade(activity_id="e", run_id="r2", symbol="SPY", side="sell"),
    ]
    counts = trades.positions_opened_per_run(rows)
    assert counts == {"r1": 2, "r2": 1}


def test_positions_opened_includes_none_run_for_manual_fills():
    rows = [
        _trade(activity_id="m1", run_id=None, symbol="DOG", side="buy"),
    ]
    counts = trades.positions_opened_per_run(rows)
    assert counts == {None: 1}


# ---------- llm_cost_per_position_for_run ----------


def test_llm_cost_per_position_equal_split():
    """Run r1 cost $0.80 / 4 positions = $0.20 per position."""
    costs = [
        {"run_id": "r1", "stage": "screen", "cost_usd": 0.10},
        {"run_id": "r1", "stage": "research", "cost_usd": 0.40},
        {"run_id": "r1", "stage": "scenarios", "cost_usd": 0.20},
        {"run_id": "r1", "stage": "construct", "cost_usd": 0.10},
    ]
    out = trades.llm_cost_per_position_for_run(
        costs, positions_per_run={"r1": 4},
    )
    assert out == pytest.approx({"r1": 0.20})


def test_llm_cost_per_position_omits_runs_with_zero_positions():
    """An all-cash cycle costs LLM tokens but opens no positions — its
    cost is system overhead, not attributable to any trade."""
    costs = [{"run_id": "all_cash", "stage": "x", "cost_usd": 0.50}]
    out = trades.llm_cost_per_position_for_run(
        costs, positions_per_run={"all_cash": 0},
    )
    assert out == {}


def test_llm_cost_per_position_skips_none_run_costs():
    """Cost rows with run_id=None are system overhead with no trade home."""
    costs = [
        {"run_id": None, "stage": "x", "cost_usd": 0.05},
        {"run_id": "r1", "stage": "y", "cost_usd": 0.20},
    ]
    out = trades.llm_cost_per_position_for_run(
        costs, positions_per_run={None: 1, "r1": 1},
    )
    # None-run cost not attributed; r1 cost is.
    assert out == pytest.approx({"r1": 0.20})


# ---------- compute_trades_pnl: closed-trade FIFO + fees + attribution ----------


def test_simple_etf_round_trip_realised_pnl():
    """Open 10 TQQQ @ $70 with $0.10 fees, close 10 @ $75 with $0.10 fees.
    Gross = (75-70) * 10 * 1 = $50. Fees = $0.20. No LLM cost → net = $49.80."""
    rows = [
        _trade(activity_id="b", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=0.10, run_id="r1"),
        _trade(activity_id="s", symbol="TQQQ", side="sell", qty=10,
               fill_price=75.0, fees_usd=0.10, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows)
    assert len(res.closed) == 1
    c = res.closed[0]
    assert c.qty == 10 and c.gross_pnl_usd == pytest.approx(50.0)
    assert c.fees_usd == pytest.approx(0.20)
    assert c.attributed_llm_cost_usd == 0.0
    assert c.net_pnl_usd == pytest.approx(49.80)
    assert c.buy_run_id == "r1"   # attribution tracks the OPEN run
    assert res.open == []


def test_inverse_etf_round_trip_no_multiplier():
    """ETF-only: gross = (sell - buy) × qty with no multiplier. Inverse
    ETFs (e.g. SQQQ) are held long like any other ETF."""
    rows = [
        _trade(activity_id="b", symbol="SQQQ", kind="etf",
               side="buy", qty=10, fill_price=8.0, fees_usd=1.0, run_id="r1"),
        _trade(activity_id="s", symbol="SQQQ", kind="etf",
               side="sell", qty=10, fill_price=9.0, fees_usd=1.0, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows)
    c = res.closed[0]
    assert c.gross_pnl_usd == pytest.approx(10.0)  # (9-8) × 10, no ×100
    assert c.fees_usd == pytest.approx(2.0)
    assert c.net_pnl_usd == pytest.approx(8.0)


def test_fifo_partial_close_emits_one_closed_row_per_chunk():
    """Two open buys, one full close should produce two closed rows
    (one per matched chunk)."""
    rows = [
        _trade(activity_id="b1", symbol="TQQQ", side="buy", qty=4,
               fill_price=70.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="b2", symbol="TQQQ", side="buy", qty=6,
               fill_price=72.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s",  symbol="TQQQ", side="sell", qty=10,
               fill_price=80.0, fees_usd=0.0, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows)
    # FIFO: oldest lot consumed first → 4 @ $70, then 6 @ $72.
    assert len(res.closed) == 2
    first, second = res.closed
    assert first.qty == 4 and first.buy_price == 70.0
    assert first.gross_pnl_usd == pytest.approx((80 - 70) * 4)
    assert second.qty == 6 and second.buy_price == 72.0
    assert second.gross_pnl_usd == pytest.approx((80 - 72) * 6)


def test_partial_close_leaves_remainder_open():
    """Buy 10, sell 4 → one closed (qty=4) + one open (qty=6)."""
    rows = [
        _trade(activity_id="b", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s", symbol="TQQQ", side="sell", qty=4,
               fill_price=80.0, fees_usd=0.0, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows, marks={"TQQQ": 85.0})
    assert len(res.closed) == 1 and res.closed[0].qty == 4
    assert len(res.open) == 1
    o = res.open[0]
    assert o.qty == 6 and o.buy_price == 70.0
    # Unrealised: ($85 - $70) * 6 = $90
    assert o.gross_pnl_usd == pytest.approx(90.0)
    assert o.mark == 85.0


def test_open_lot_with_no_mark_has_none_gross():
    """Unmarked open lot → gross=None so the dashboard renders '—'."""
    rows = [
        _trade(activity_id="b", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=0.0, run_id="r1"),
    ]
    res = trades.compute_trades_pnl(rows, marks={})
    assert res.open[0].gross_pnl_usd is None
    assert res.open[0].net_pnl_usd is None


def test_attribution_equal_split_across_positions_opened_in_run():
    """Run r1 opens 2 positions and cost $0.80 → $0.40 per position.
    Each closed trade carries its OWN opening run's allocation."""
    costs = [{"run_id": "r1", "stage": "x", "cost_usd": 0.80}]
    rows = [
        _trade(activity_id="b1", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="b2", symbol="SOXL", side="buy", qty=5,
               fill_price=40.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s1", symbol="TQQQ", side="sell", qty=10,
               fill_price=72.0, fees_usd=0.0, run_id="r2"),
        _trade(activity_id="s2", symbol="SOXL", side="sell", qty=5,
               fill_price=44.0, fees_usd=0.0, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows, costs=costs)
    by_sym = {c.symbol: c for c in res.closed}
    assert by_sym["TQQQ"].attributed_llm_cost_usd == pytest.approx(0.40)
    assert by_sym["SOXL"].attributed_llm_cost_usd == pytest.approx(0.40)
    # Net = gross - fees - LLM
    assert by_sym["TQQQ"].net_pnl_usd == pytest.approx((72 - 70) * 10 - 0 - 0.40)
    assert by_sym["SOXL"].net_pnl_usd == pytest.approx((44 - 40) * 5 - 0 - 0.40)


def test_attribution_sums_to_full_run_cost_across_partial_closes():
    """A 10-share lot closed in two halves must attribute exactly the full
    $0.40 allocation (not $0.80 by double-counting, not $0.20 by missing
    half). Sum of attributed cost across all closes for that lot must
    equal the run's per-position allocation."""
    costs = [{"run_id": "r1", "stage": "x", "cost_usd": 0.40}]
    rows = [
        _trade(activity_id="b", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s1", symbol="TQQQ", side="sell", qty=4,
               fill_price=75.0, fees_usd=0.0, run_id="r2"),
        _trade(activity_id="s2", symbol="TQQQ", side="sell", qty=6,
               fill_price=80.0, fees_usd=0.0, run_id="r3"),
    ]
    res = trades.compute_trades_pnl(rows, costs=costs)
    total_attributed = sum(c.attributed_llm_cost_usd for c in res.closed)
    assert total_attributed == pytest.approx(0.40)
    # And proportional to chunk size
    by_chunk = {c.sell_activity_id: c.attributed_llm_cost_usd for c in res.closed}
    assert by_chunk["s1"] == pytest.approx(0.40 * 4 / 10)
    assert by_chunk["s2"] == pytest.approx(0.40 * 6 / 10)


def test_attribution_zero_for_lots_with_none_run_id():
    """Operator-placed fills (run_id=None) get no LLM attribution — they
    were nothing to do with the agent's research cycles."""
    costs = [{"run_id": "r1", "stage": "x", "cost_usd": 1.00}]
    rows = [
        _trade(activity_id="b", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=0.0, run_id=None),
        _trade(activity_id="s", symbol="TQQQ", side="sell", qty=10,
               fill_price=75.0, fees_usd=0.0, run_id=None),
    ]
    res = trades.compute_trades_pnl(rows, costs=costs)
    assert res.closed[0].attributed_llm_cost_usd == 0.0
    assert res.closed[0].buy_run_id is None


def test_fees_are_prorated_when_lot_is_partially_closed():
    """Buy 10 with $1.00 fees, sell 4 → that close should carry $0.40 of
    buy-side fees (4/10), leaving $0.60 on the remaining open lot."""
    rows = [
        _trade(activity_id="b", symbol="TQQQ", side="buy", qty=10,
               fill_price=70.0, fees_usd=1.00, run_id="r1"),
        _trade(activity_id="s", symbol="TQQQ", side="sell", qty=4,
               fill_price=75.0, fees_usd=0.20, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows)
    c = res.closed[0]
    # buy_fees_share = 1.00 * 4/10 = 0.40; sell_fees = 0.20
    assert c.fees_usd == pytest.approx(0.60)
    # Remaining open lot keeps $0.60 of buy-side fees.
    o = res.open[0]
    assert o.fees_usd == pytest.approx(0.60)


# ---------- TradesPnl totals ----------


def test_totals_sum_across_closed_trades():
    rows = [
        _trade(activity_id="b1", symbol="A", side="buy", qty=1,
               fill_price=10.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s1", symbol="A", side="sell", qty=1,
               fill_price=15.0, fees_usd=0.0, run_id="r2"),
        _trade(activity_id="b2", symbol="B", side="buy", qty=1,
               fill_price=20.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s2", symbol="B", side="sell", qty=1,
               fill_price=18.0, fees_usd=0.0, run_id="r3"),
    ]
    costs = [{"run_id": "r1", "stage": "x", "cost_usd": 1.00}]
    res = trades.compute_trades_pnl(rows, costs=costs)
    # r1 opened 2 positions → $0.50 each
    assert res.total_realised_gross_usd == pytest.approx(5.0 + (-2.0))
    assert res.total_realised_llm_cost_usd == pytest.approx(1.0)
    assert res.total_realised_net_usd == pytest.approx(3.0 - 1.0)


# ---------- Unmatched sells (Codex P1 on PR #79) ----------


def test_unmatched_sells_empty_when_all_sells_fifo_match():
    """Round-trip buy/sell leaves no unmatched residue."""
    rows = [
        _trade(activity_id="b1", symbol="A", side="buy", qty=2,
               fill_price=10.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s1", symbol="A", side="sell", qty=2,
               fill_price=12.0, fees_usd=0.0, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows)
    assert res.unmatched_sells == []


def test_unmatched_sells_records_sell_without_prior_buy():
    """A sell fill with no open buy lot lands in unmatched_sells —
    pre-PR-#79 these were silently dropped."""
    rows = [
        _trade(activity_id="s1", symbol="A", side="sell", qty=3,
               fill_price=12.0, fees_usd=0.0, run_id="r1"),
    ]
    res = trades.compute_trades_pnl(rows)
    assert len(res.unmatched_sells) == 1
    u = res.unmatched_sells[0]
    assert u.symbol == "A"
    assert u.qty == pytest.approx(3.0)
    assert u.fill_price == pytest.approx(12.0)
    assert u.activity_id == "s1"


def test_unmatched_sells_records_leftover_after_partial_match():
    """Buy 1, sell 3 → 1 unit closes cleanly, 2 units land as
    unmatched_sells. The closed-trade list captures the 1-unit
    match, the unmatched_sells list captures the leftover."""
    rows = [
        _trade(activity_id="b1", symbol="A", side="buy", qty=1,
               fill_price=10.0, fees_usd=0.0, run_id="r1"),
        _trade(activity_id="s1", symbol="A", side="sell", qty=3,
               fill_price=12.0, fees_usd=0.0, run_id="r2"),
    ]
    res = trades.compute_trades_pnl(rows)
    assert len(res.closed) == 1
    assert res.closed[0].qty == pytest.approx(1.0)
    assert len(res.unmatched_sells) == 1
    assert res.unmatched_sells[0].qty == pytest.approx(2.0)


# ---------- symbols_in_cooldown ----------


from datetime import datetime, timezone  # noqa: E402


def _now(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_cooldown_includes_symbol_fully_exited_within_window():
    rows = [
        _trade(activity_id="b1", side="buy", qty=10, fill_price=50.0,
               filled_at="2026-05-10T15:00:00Z"),
        _trade(activity_id="s1", side="sell", qty=10, fill_price=55.0,
               filled_at="2026-05-24T15:00:00Z"),  # full exit
    ]
    out = trades.symbols_in_cooldown(rows, now=_now("2026-05-26T15:00:00Z"), window_days=7)
    assert out == {"TQQQ": "2026-05-24T15:00:00Z"}


def test_cooldown_excludes_exit_older_than_window():
    rows = [
        _trade(activity_id="b1", side="buy", qty=10, fill_price=50.0,
               filled_at="2026-04-01T15:00:00Z"),
        _trade(activity_id="s1", side="sell", qty=10, fill_price=55.0,
               filled_at="2026-05-10T15:00:00Z"),  # 16 days before now
    ]
    out = trades.symbols_in_cooldown(rows, now=_now("2026-05-26T15:00:00Z"), window_days=7)
    assert out == {}


def test_cooldown_excludes_currently_open_symbol():
    """A still-open position is a continuing hold, not a re-entry — never in cooldown."""
    rows = [
        _trade(activity_id="b1", side="buy", qty=10, fill_price=50.0,
               filled_at="2026-05-10T15:00:00Z"),
        _trade(activity_id="s1", side="sell", qty=4, fill_price=55.0,
               filled_at="2026-05-24T15:00:00Z"),  # partial — 6 remain open
    ]
    out = trades.symbols_in_cooldown(rows, now=_now("2026-05-26T15:00:00Z"), window_days=7)
    assert out == {}


def test_cooldown_reopened_symbol_not_flagged():
    """Closed then reopened within the window: currently open again, so not
    a cooldown blocker for the position it already holds."""
    rows = [
        _trade(activity_id="b1", side="buy", qty=10, fill_price=50.0,
               filled_at="2026-05-10T15:00:00Z"),
        _trade(activity_id="s1", side="sell", qty=10, fill_price=55.0,
               filled_at="2026-05-24T15:00:00Z"),
        _trade(activity_id="b2", side="buy", qty=8, fill_price=56.0,
               filled_at="2026-05-25T15:00:00Z"),  # reopened — now open
    ]
    out = trades.symbols_in_cooldown(rows, now=_now("2026-05-26T15:00:00Z"), window_days=7)
    assert out == {}


def test_cooldown_robust_to_out_of_order_log():
    """A sell appended ahead of its earlier buy (out-of-order sync) must
    still FIFO-match: the symbol is fully exited, so it IS in cooldown.
    Without the chronological sort the buy would be a phantom open lot and
    the symbol would be wrongly excluded."""
    rows = [
        _trade(activity_id="s1", side="sell", qty=10, fill_price=55.0,
               filled_at="2026-05-24T15:00:00Z"),   # logged first (out of order)
        _trade(activity_id="b1", side="buy", qty=10, fill_price=50.0,
               filled_at="2026-05-10T15:00:00Z"),   # actually earlier
    ]
    out = trades.symbols_in_cooldown(rows, now=_now("2026-05-26T15:00:00Z"), window_days=7)
    assert out == {"TQQQ": "2026-05-24T15:00:00Z"}
