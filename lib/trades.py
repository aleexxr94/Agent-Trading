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
  - FIFO per (mode, symbol). A sell consumes the oldest open buy lot of the
    SAME account era first — paper and live Alpaca accounts do not share
    inventory, so a live sell must never close a leftover paper lot (Codex
    P1 on PR #112). Rows without a mode tag predate era-tagging and are all
    paper, so paper-only history matches exactly as before.
  - Partial closes produce one CLOSED row per matched chunk; the
    remaining qty stays open.
  - ETF-only: gross PnL is (sell - buy) × qty, no multiplier.

Out of scope (this PR):
  - Alpaca activities sync (writing into trades.jsonl from live fills)
  - Dashboard rendering
  - Order-flow integration with the orchestrator
Those land in separate PRs to keep review surface small.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True)
class Lot:
    """One open buy lot — a fill that hasn't yet been fully matched by a sell."""
    activity_id: str
    run_id: str | None
    symbol: str
    kind: Literal["etf"]
    qty: float            # remaining (unmatched) qty
    fill_price: float
    fees_usd: float       # remaining fees pro-rated by remaining qty
    filled_at: str


@dataclass(frozen=True)
class ClosedTrade:
    """One matched (buy lot ↔ sell fill) pair — possibly a partial close.

    ``gross_pnl_usd`` = (sell_price - buy_price) * qty.
    ``fees_usd`` = buy_fees_share + sell_fees_share (regulatory; real on live,
    modelled on paper by lib.alpaca_costs).
    ``slippage_usd`` = buy_slip_share + sell_slip_share (modelled spread cost,
    both legs).
    ``attributed_llm_cost_usd`` = sum of equal-split allocations for the
    OPENING run (we attribute only to the run that put the position on,
    per the locked methodology).
    ``net_pnl_usd`` = gross - fees - slippage - attributed_llm_cost.
    """
    symbol: str
    kind: Literal["etf"]
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
    slippage_usd: float = 0.0
    mode: str = "paper"   # account era; both legs share it (era-split FIFO)


@dataclass(frozen=True)
class OpenTradePnl:
    """Unrealised PnL on a still-open lot. Marks-derived, fees are the
    buy-side fees only (the sell hasn't happened yet)."""
    symbol: str
    kind: Literal["etf"]
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
    slippage_usd: float = 0.0        # buy-side only (entry leg)
    mode: str = "paper"              # account era the lot belongs to


@dataclass(frozen=True)
class UnmatchedSell:
    """A sell fill that couldn't be FIFO-matched against any open buy
    lot when compute_trades_pnl walked the trade log. The system spec
    is "no broker shorts", so this should never happen in healthy
    operation. When it does, it signals data loss / out-of-order
    activities sync / a legacy fill predating our trade-sync window /
    a manual edit to trades.jsonl. Surfaced via TradesPnl so the
    dashboard can warn the operator instead of silently misrepresenting
    the synthetic balance.
    """
    symbol: str
    kind: Literal["etf"]
    qty: float
    fill_price: float
    filled_at: str
    activity_id: str


@dataclass(frozen=True)
class TradesPnl:
    closed: list[ClosedTrade]
    open: list[OpenTradePnl]
    unmatched_sells: list[UnmatchedSell] = field(default_factory=list)

    @property
    def total_realised_gross_usd(self) -> float:
        return sum(t.gross_pnl_usd for t in self.closed)

    @property
    def total_realised_fees_usd(self) -> float:
        return sum(t.fees_usd for t in self.closed)

    @property
    def total_realised_slippage_usd(self) -> float:
        return sum(t.slippage_usd for t in self.closed)

    @property
    def total_realised_llm_cost_usd(self) -> float:
        return sum(t.attributed_llm_cost_usd for t in self.closed)

    @property
    def total_realised_net_usd(self) -> float:
        return sum(t.net_pnl_usd for t in self.closed)


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

    ``trades`` must be ordered chronologically — callers sort by
    ``filled_at`` first (see feedback.build_performance_memo,
    trades.recent_exit_cooldowns, dashboard_data.trades_pnl_view). File
    order is NOT chronological: trades_sync appends fills grouped by
    order_id, so raw ``state.read_trades()`` output can interleave a
    sell before its earlier-filled buy and produce spurious unmatched
    sells. Within each symbol we FIFO-match sells against buy lots.

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

    # FIFO queues per (mode, symbol) of open buy lots (with remaining qty +
    # remaining fees). Keyed by era so cross-account matching is impossible.
    open_lots: dict[tuple[str, str], deque[dict]] = defaultdict(deque)
    closed: list[ClosedTrade] = []
    # Sells that couldn't FIFO-match against an open buy lot. Should
    # be empty in healthy operation (spec is "no broker shorts").
    # Codex P1 on PR #79: surfaced so the dashboard can warn the
    # operator instead of silently corrupting the synthetic balance.
    unmatched_sells: list[UnmatchedSell] = []

    for t in trades:
        symbol = t["symbol"]
        kind = t["kind"]
        side = t["side"]
        qty = float(t["qty"])
        price = float(t["fill_price"])
        fees = float(t.get("fees_usd", 0.0) or 0.0)
        slippage = float(t.get("slippage_usd", 0.0) or 0.0)
        run_id = t.get("run_id")
        filled_at = t.get("filled_at", "")
        activity_id = t.get("activity_id", "")
        lot_key = (t.get("mode") or "paper", symbol)

        if side == "buy":
            open_lots[lot_key].append({
                "activity_id": activity_id,
                "run_id": run_id,
                "kind": kind,
                "remaining_qty": qty,
                "original_qty": qty,
                "fill_price": price,
                "remaining_fees": fees,
                "remaining_slippage": slippage,
                "filled_at": filled_at,
            })
            continue

        # side == "sell": consume oldest open lots FIFO.
        remaining_sell_qty = qty
        # Pro-rate the sell-side fees + slippage by qty as we consume lots.
        sell_fees_per_unit = (fees / qty) if qty > 0 else 0.0
        sell_slip_per_unit = (slippage / qty) if qty > 0 else 0.0
        while remaining_sell_qty > 1e-9 and open_lots[lot_key]:
            lot = open_lots[lot_key][0]
            matched = min(remaining_sell_qty, lot["remaining_qty"])
            # Pro-rate buy-side fees + slippage by matched fraction of the lot.
            buy_fees_share = (
                lot["remaining_fees"] * (matched / lot["remaining_qty"])
                if lot["remaining_qty"] > 0 else 0.0
            )
            buy_slip_share = (
                lot["remaining_slippage"] * (matched / lot["remaining_qty"])
                if lot["remaining_qty"] > 0 else 0.0
            )
            sell_fees_share = sell_fees_per_unit * matched
            sell_slip_share = sell_slip_per_unit * matched
            gross = (price - lot["fill_price"]) * matched
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
            total_slippage = buy_slip_share + sell_slip_share
            net = gross - total_fees - total_slippage - llm_share
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
                slippage_usd=total_slippage,
                mode=lot_key[0],
            ))
            lot["remaining_qty"] -= matched
            lot["remaining_fees"] -= buy_fees_share
            lot["remaining_slippage"] -= buy_slip_share
            remaining_sell_qty -= matched
            if lot["remaining_qty"] <= 1e-9:
                open_lots[lot_key].popleft()
        # If remaining_sell_qty > 0 here, the sell fill couldn't be
        # FIFO-matched against any open buy lot. System spec is "no
        # broker shorts", so this should never happen in healthy
        # operation. When it does, it signals: out-of-order activities
        # sync, a legacy fill predating our trade-sync window, manual
        # editing of trades.jsonl, or actual data loss.
        #
        # Pre-PR #79 we silently dropped these and let the dashboard
        # silently misrepresent totals. Now we record them on TradesPnl
        # so SyntheticBalance can surface a warning to the operator
        # rather than corrupt the headline balance invisibly.
        if remaining_sell_qty > 1e-9:
            unmatched_sells.append(UnmatchedSell(
                symbol=symbol,
                kind=kind,
                qty=remaining_sell_qty,
                fill_price=price,
                filled_at=filled_at,
                activity_id=activity_id,
            ))

    # Flatten remaining open lots into OpenTradePnl rows. The era is kept
    # on the row (Codex P2 on PR #112) so consumers judging "currently
    # open" — the re-entry cooldown above all — can scope inventory to the
    # account they actually trade in.
    open_rows: list[OpenTradePnl] = []
    for (lot_mode, symbol), lots in open_lots.items():
        for lot in lots:
            mark = marks.get(symbol)
            gross = (
                (mark - lot["fill_price"]) * lot["remaining_qty"]
                if mark is not None else None
            )
            llm_alloc = per_position_cost.get(lot["run_id"], 0.0) if lot["run_id"] else 0.0
            llm_share = (
                llm_alloc * (lot["remaining_qty"] / lot["original_qty"])
                if lot["original_qty"] > 0 else 0.0
            )
            net = (
                gross - lot["remaining_fees"] - lot["remaining_slippage"] - llm_share
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
                slippage_usd=lot["remaining_slippage"],
                mode=lot_mode,
            ))

    return TradesPnl(
        closed=closed, open=open_rows, unmatched_sells=unmatched_sells,
    )


def _parse_iso_utc(s: str | None) -> datetime | None:
    """Tolerant ISO-8601 → aware-UTC parse. Returns None on anything bad."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def symbols_in_cooldown(
    trades: list[dict],
    *,
    now: datetime,
    window_days: float,
    mode: str = "paper",
) -> dict[str, str]:
    """Symbols that were FULLY exited within ``window_days`` of ``now``.

    Returns ``{symbol: last_exit_iso}`` for each symbol that currently has
    NO open lots (i.e. it was fully closed) AND whose most-recent close
    happened within the cooldown window. Re-opening one of these symbols
    is a "re-entry" the cooldown is meant to discourage (overridable by
    high re-entry confidence — see ``risk.REENTRY_COOLDOWN_OVERRIDE_CONFIDENCE``).

    Keyed by the broker symbol (ETF ticker), matching the convention used
    by ``compute_trades_pnl`` and the rest of the system.
    A symbol that is currently open (any remaining lot) is never in
    cooldown — that's a continuing hold, not a re-entry, so unrelated /
    still-held positions are not blocked.

    ``trades`` is sorted by ``filled_at`` before FIFO-matching: the log is
    append-order and a sync can append a sell ahead of its earlier buy,
    which would otherwise leave the buy as a phantom open lot and wrongly
    exclude a just-exited symbol from cooldown.

    ``mode`` is the CURRENT account era. Exits from ANY era start the
    cooldown clock (the 7-day window deliberately spans the paper→live
    boundary), but a close only counts as an exit when it FULLY closed its
    own era's inventory — a partial paper sell with the rest of the paper
    lot still open was never a full exit and must not block live entries
    (Codex P2 on PR #112). "Currently open" is judged against the CURRENT
    era only, so a leftover paper lot can't make a fully-exited live symbol
    look like a continuing hold (also Codex P2 on PR #112).
    """
    rows = sorted(trades, key=lambda r: r.get("filled_at") or "")
    pnl = compute_trades_pnl(rows)
    open_by_era: dict[str, set[str]] = defaultdict(set)
    for lot in pnl.open:
        open_by_era[lot.mode].add(lot.symbol)
    open_symbols = open_by_era.get(mode, set())
    last_exit: dict[str, str] = {}
    for ct in pnl.closed:
        if ct.symbol in open_by_era.get(ct.mode, set()):
            continue  # not a full exit within the close's own era
        if ct.symbol in open_symbols:
            continue  # currently held in the trading era — a continuing hold
        prev = last_exit.get(ct.symbol)
        if prev is None or ct.closed_at > prev:
            last_exit[ct.symbol] = ct.closed_at
    out: dict[str, str] = {}
    for sym, exit_iso in last_exit.items():
        exit_dt = _parse_iso_utc(exit_iso)
        if exit_dt is None:
            continue
        age_days = (now - exit_dt).total_seconds() / 86400.0
        if 0 <= age_days <= window_days:
            out[sym] = exit_iso
    return out
