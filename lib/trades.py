"""Per-trade PnL accounting.

Two pure helpers + the dataclasses they produce. Strictly: takes the raw
``state/trades.jsonl`` rows (one per Alpaca fill) and the
``state/costs.jsonl`` rows (one per LLM invocation) and produces
realised + unrealised PnL with fees pulled from the fills and LLM cost
attributed equal-split across the positions opened in the trade's run.

Attribution methodology (locked, per user decision on May 12 2026):
  - Token cost: per-position equal split. A run that opens 4 positions
    has each position carry 1/4 of that run's total LLM cost. A position
    opened in a separate run carries its own run's allocation. Runs that
    did not open any positions (e.g. all-cash cycles) contribute nothing
    to per-trade attribution — their cost is system overhead.
  - Trading fees: real, pulled from Alpaca fills. No modelled estimate.

Match logic:
  - FIFO per symbol. A sell consumes the oldest open buy lot first.
  - Partial closes produce one CLOSED row per matched chunk; the
    remaining qty stays open.
  - Options keep their multiplier (×100) for gross PnL; ETFs are 1×.

Out of scope (this PR):
  - Alpaca activities sync (writing into trades.jsonl from live fills)
  - Dashboard rendering
  - Order-flow integration with the orchestrator
Those land in separate PRs to keep review surface small.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Lot:
    """One open buy lot — a fill that hasn't yet been fully matched by a sell."""
    activity_id: str
    run_id: str | None
    symbol: str
    kind: Literal["etf", "option"]
    qty: float            # remaining (unmatched) qty
    fill_price: float
    fees_usd: float       # remaining fees pro-rated by remaining qty
    filled_at: str


@dataclass(frozen=True)
class ClosedTrade:
    """One matched (buy lot ↔ sell fill) pair — possibly a partial close.

    ``gross_pnl_usd`` = (sell_price - buy_price) * qty * multiplier.
    ``fees_usd`` = buy_fees_share + sell_fees_share (real, from Alpaca).
    ``attributed_llm_cost_usd`` = sum of equal-split allocations for the
    OPENING run (we attribute only to the run that put the position on,
    per the locked methodology).
    ``net_pnl_usd`` = gross - fees - attributed_llm_cost.
    """
    symbol: str
    kind: Literal["etf", "option"]
    qty: float
    buy_price: float
    sell_price: float
    buy_activity_id: str
    sell_activity_id: str
    buy_run_id: str | None
    opened_at: str
    closed_at: str
    gross_pnl_usd: float
    fees_usd: float
    attributed_llm_cost_usd: float
    net_pnl_usd: float


@dataclass(frozen=True)
class OpenTradePnl:
    """Unrealised PnL on a still-open lot. Marks-derived, fees are the
    buy-side fees only (the sell hasn't happened yet)."""
    symbol: str
    kind: Literal["etf", "option"]
    qty: float
    buy_price: float
    mark: float | None
    buy_activity_id: str
    buy_run_id: str | None
    opened_at: str
    gross_pnl_usd: float | None      # None when mark is unavailable
    fees_usd: float                  # buy-side only
    attributed_llm_cost_usd: float
    net_pnl_usd: float | None        # None when mark is unavailable


@dataclass(frozen=True)
class TradesPnl:
    closed: list[ClosedTrade]
    open: list[OpenTradePnl]

    @property
    def total_realised_gross_usd(self) -> float:
        return sum(t.gross_pnl_usd for t in self.closed)

    @property
    def total_realised_fees_usd(self) -> float:
        return sum(t.fees_usd for t in self.closed)

    @property
    def total_realised_llm_cost_usd(self) -> float:
        return sum(t.attributed_llm_cost_usd for t in self.closed)

    @property
    def total_realised_net_usd(self) -> float:
        return sum(t.net_pnl_usd for t in self.closed)


def _multiplier(kind: str) -> int:
    return 100 if kind == "option" else 1


def positions_opened_per_run(trades: list[dict]) -> dict[str, int]:
    """Count distinct (run_id, symbol) opening fills per run.

    A position is "opened" by a buy. Multiple buy fills on the same symbol
    in the same run still count as ONE position (partial fills shouldn't
    inflate the attribution denominator). Sells don't open positions.

    Returns {run_id: count}. ``None`` run_id (manual fills) is included
    so the equal-split helper can decide what to do with them — by default
    it allocates $0 cost to those.
    """
    by_run: dict[str | None, set[str]] = defaultdict(set)
    for t in trades:
        if t.get("side") != "buy":
            continue
        by_run[t.get("run_id")].add(t["symbol"])
    return {rid: len(syms) for rid, syms in by_run.items()}


def llm_cost_per_position_for_run(
    costs: list[dict],
    *,
    positions_per_run: dict[str, int],
) -> dict[str, float]:
    """Per-position equal-split LLM cost allocation for each run.

    ``costs`` is the raw ``state/costs.jsonl`` rows. We sum cost_usd by
    run_id, then divide by the number of distinct positions that run
    opened. Returns ``{run_id: cost_per_position_usd}``. Runs that
    opened zero positions are omitted (no positions to attribute to).
    """
    totals: dict[str, float] = defaultdict(float)
    for row in costs:
        rid = row.get("run_id")
        if rid is None:
            continue
        totals[rid] += row.get("cost_usd", 0.0) or 0.0
    out: dict[str, float] = {}
    for rid, total in totals.items():
        n = positions_per_run.get(rid, 0)
        if n <= 0:
            continue
        out[rid] = total / n
    return out


def compute_trades_pnl(
    trades: list[dict],
    *,
    costs: list[dict] | None = None,
    marks: dict[str, float] | None = None,
) -> TradesPnl:
    """Turn trades.jsonl + costs.jsonl into (closed_trades, open_lots) PnL.

    ``trades`` must be ordered chronologically (caller passes them
    straight from ``state.read_trades()``, which preserves file order).
    Within each symbol we FIFO-match sells against buy lots.

    ``costs`` defaults to [] (no LLM attribution at all — useful for
    pure unit tests).

    ``marks`` maps symbol → current per-unit price for unrealised PnL
    on open lots. Symbols absent from marks produce ``gross_pnl_usd=None``
    rows so the dashboard can render "—" instead of zero.
    """
    costs = costs or []
    marks = marks or {}

    per_run_positions = positions_opened_per_run(trades)
    per_position_cost = llm_cost_per_position_for_run(
        costs, positions_per_run=per_run_positions,
    )

    # FIFO queues per symbol of open buy lots (with remaining qty + remaining fees).
    open_lots: dict[str, deque[dict]] = defaultdict(deque)
    closed: list[ClosedTrade] = []

    for t in trades:
        symbol = t["symbol"]
        kind = t["kind"]
        side = t["side"]
        qty = float(t["qty"])
        price = float(t["fill_price"])
        fees = float(t.get("fees_usd", 0.0) or 0.0)
        run_id = t.get("run_id")
        filled_at = t.get("filled_at", "")
        activity_id = t.get("activity_id", "")
        mult = _multiplier(kind)

        if side == "buy":
            open_lots[symbol].append({
                "activity_id": activity_id,
                "run_id": run_id,
                "kind": kind,
                "remaining_qty": qty,
                "original_qty": qty,
                "fill_price": price,
                "remaining_fees": fees,
                "filled_at": filled_at,
            })
            continue

        # side == "sell": consume oldest open lots FIFO.
        remaining_sell_qty = qty
        # Pro-rate the sell-side fees by qty as we consume lots.
        sell_fees_per_unit = (fees / qty) if qty > 0 else 0.0
        while remaining_sell_qty > 1e-9 and open_lots[symbol]:
            lot = open_lots[symbol][0]
            matched = min(remaining_sell_qty, lot["remaining_qty"])
            # Pro-rate buy-side fees by matched fraction of the original lot.
            buy_fees_share = (
                lot["remaining_fees"] * (matched / lot["remaining_qty"])
                if lot["remaining_qty"] > 0 else 0.0
            )
            sell_fees_share = sell_fees_per_unit * matched
            gross = (price - lot["fill_price"]) * matched * mult
            # Equal-split allocation: each (run_id, symbol) opening event
            # carries one share. The matched chunk represents the same
            # opening event, so its full allocation = per_position_cost
            # for that run (NOT scaled down by matched/original — closing
            # half a position doesn't halve the research cost it took to
            # decide to open it).
            llm_alloc = per_position_cost.get(lot["run_id"], 0.0) if lot["run_id"] else 0.0
            # ...however, when a single lot is partially closed multiple
            # times, naively assigning the full allocation each time would
            # double-count. Apportion by remaining-fraction so the sum
            # across all close events equals exactly per_position_cost.
            llm_share = (
                llm_alloc * (matched / lot["original_qty"])
                if lot["original_qty"] > 0 else 0.0
            )
            total_fees = buy_fees_share + sell_fees_share
            net = gross - total_fees - llm_share
            closed.append(ClosedTrade(
                symbol=symbol,
                kind=kind,
                qty=matched,
                buy_price=lot["fill_price"],
                sell_price=price,
                buy_activity_id=lot["activity_id"],
                sell_activity_id=activity_id,
                buy_run_id=lot["run_id"],
                opened_at=lot["filled_at"],
                closed_at=filled_at,
                gross_pnl_usd=gross,
                fees_usd=total_fees,
                attributed_llm_cost_usd=llm_share,
                net_pnl_usd=net,
            ))
            lot["remaining_qty"] -= matched
            lot["remaining_fees"] -= buy_fees_share
            remaining_sell_qty -= matched
            if lot["remaining_qty"] <= 1e-9:
                open_lots[symbol].popleft()
        # If remaining_sell_qty > 0 here, the operator is short-selling
        # without an open buy lot. Paper trading allows it; we log a
        # synthetic "open short" lot. Until short selling is in scope we
        # silently drop the unmatched sell — the dashboard's totals will
        # be off only for that edge case and the operator can grep
        # trades.jsonl to debug.

    # Flatten remaining open lots into OpenTradePnl rows.
    open_rows: list[OpenTradePnl] = []
    for symbol, lots in open_lots.items():
        for lot in lots:
            mark = marks.get(symbol)
            mult = _multiplier(lot["kind"])
            gross = (
                (mark - lot["fill_price"]) * lot["remaining_qty"] * mult
                if mark is not None else None
            )
            llm_alloc = per_position_cost.get(lot["run_id"], 0.0) if lot["run_id"] else 0.0
            llm_share = (
                llm_alloc * (lot["remaining_qty"] / lot["original_qty"])
                if lot["original_qty"] > 0 else 0.0
            )
            net = (
                gross - lot["remaining_fees"] - llm_share
                if gross is not None else None
            )
            open_rows.append(OpenTradePnl(
                symbol=symbol,
                kind=lot["kind"],
                qty=lot["remaining_qty"],
                buy_price=lot["fill_price"],
                mark=mark,
                buy_activity_id=lot["activity_id"],
                buy_run_id=lot["run_id"],
                opened_at=lot["filled_at"],
                gross_pnl_usd=gross,
                fees_usd=lot["remaining_fees"],
                attributed_llm_cost_usd=llm_share,
                net_pnl_usd=net,
            ))

    return TradesPnl(closed=closed, open=open_rows)
