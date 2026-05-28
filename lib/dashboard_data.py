"""Pure data-layer helpers for the dashboard.

Separated from dashboard.py so they can be unit-tested without streamlit
installed. Keep this file streamlit-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import pnl as pnl_lib
from . import state
from . import universe as universe_lib
from .orders import osi_symbol

ROOT = Path(__file__).resolve().parent.parent
SEED_PORTFOLIO_FALLBACK = ROOT / "tests" / "fixtures" / "portfolio.json"


def load_portfolio() -> tuple[dict, str]:
    """Return (portfolio_dict, source_label).

    Prefers state/current_portfolio.json. If absent, falls back to
    state/seed_portfolio.json, then to the bundled fixture so the dashboard
    always has something to render on a fresh checkout.
    """
    if state.CURRENT_PORTFOLIO.exists():
        return state.read_json(state.CURRENT_PORTFOLIO), "live"
    seed = state.STATE_DIR / "seed_portfolio.json"
    if seed.exists():
        return state.read_json(seed), "seed"
    if SEED_PORTFOLIO_FALLBACK.exists():
        return json.loads(SEED_PORTFOLIO_FALLBACK.read_text()), "fixture"
    return _empty_portfolio(), "empty"


def _empty_portfolio() -> dict:
    return {
        "run_id": "none",
        "generated_at": state.utcnow_iso(),
        "nav_usd": 2500.0,
        "cash_usd": 2500.0,
        "cash_buffer_pct": 100.0,
        "all_cash": True,
        "all_cash_rationale": "No portfolio data available — orchestrator has not run.",
        "positions": [],
        "construction_rationale": "n/a",
    }


def load_decisions(limit: int = 100) -> list[dict]:
    if not state.DECISIONS_LOG.exists():
        return []
    rows = []
    for line in state.DECISIONS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def load_costs(limit: int = 1000) -> list[dict]:
    """Read state/costs.jsonl, honouring the all-time reset marker.

    When the operator hits "Reset all LLM costs" on the dashboard,
    state.set_all_time_cost_reset stamps a UTC timestamp; every row
    whose `at` is ≤ that timestamp is excluded from the returned list.
    The underlying audit log on disk is never mutated.

    Cap enforcement in lib.llm.check_caps_or_raise does NOT go through
    this function — it reads state.read_costs_today / read_costs_for_run
    directly so per-run and per-day caps stay on the raw log even after
    a display reset.
    """
    if not state.COSTS_LOG.exists():
        return []
    rows = []
    for line in state.COSTS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows = state.filter_costs_post_reset(rows)
    return rows[-limit:]


def realised_llm_cost_attributed_to_trades_usd(
    marks: dict[str, float] | None = None,
) -> float:
    """LLM cost attributed to closed AND open trades, post-reset.

    The reset marker zeroes this value (via filter_costs_post_reset
    inside trades_pnl_view) — so subtracting this from a "Net P&L"
    display surface gives the operator a visible delta when they
    click 'Reset all LLM costs'.

    Closed + open lots are both summed because the equal-split
    methodology attributes the run's LLM spend at *open* time, not
    close — open positions already carry their share even before they
    realise P&L.
    """
    view = trades_pnl_view(marks=marks)
    closed_sum = sum(
        r.get("llm_cost_usd", 0.0) or 0.0 for r in view["closed"]
    )
    open_sum = sum(
        r.get("llm_cost_usd", 0.0) or 0.0 for r in view["open"]
    )
    return closed_sum + open_sum


def apply_nav_offset_to_history(
    rows: list[dict],
    *,
    nav_offset_usd: float,
    virtual_baseline_usd: float = 2500.0,
) -> list[dict]:
    """Subtract `nav_offset_usd` from each row's `nav_usd` field, but
    only when the row is in raw-broker units. Rows with
    `nav_source == "virtual"` are left untouched — they were written
    under VIRTUAL_NAV_USD and are already in display units.

    For legacy rows with no `nav_source` stamp, a value-based heuristic
    is used: if 0 < nav_usd < virtual_baseline × 10 the row looks like
    virtual; anything larger is treated as broker.

    Returns a new list of shallow-copied rows (the original list and
    its rows are not mutated). When `nav_offset_usd == 0` the input is
    returned as-is.

    Fixes the Y-axis = -$95k bug the operator hit when an anchor was
    set (offset $97,527) but the orchestrator was already writing
    rows in virtual units ($2,500), so subtracting again landed the
    chart at -$95,027.
    """
    if not nav_offset_usd:
        return rows
    out: list[dict] = []
    threshold = virtual_baseline_usd * 10
    for row in rows:
        src = row.get("nav_source")
        nav = row.get("nav_usd")
        if not isinstance(nav, (int, float)):
            out.append(dict(row))
            continue
        if src == "virtual":
            is_virtual = True
        elif src == "broker":
            is_virtual = False
        else:
            # Legacy row, no stamp — guess by magnitude.
            is_virtual = 0 < nav < threshold
        new_row = dict(row)
        if not is_virtual:
            new_row["nav_usd"] = nav - nav_offset_usd
        out.append(new_row)
    return out


def settled_balance_usd(
    *,
    virtual_baseline_usd: float = 2500.0,
    marks: dict[str, float] | None = None,
) -> float:
    """Backward-compat wrapper around the new synthetic-balance helper.

    Returns the synthetic balance computed with empty marks — i.e. the
    "realized only" view: $2,500 + closed gross P&L − LLM cost
    − trading fees, with open-position gross P&L excluded. This matches
    the original definition of the Settled balance card (closed trades
    only, frozen between closes).

    For the live mark-aware balance used in the hero card, callers
    should reach for `compute_synthetic_balance(marks=broker_marks)`
    directly. This wrapper exists so the existing chips strip and
    several unit tests written against `settled_balance_usd` keep
    working unchanged.
    """
    return compute_synthetic_balance(
        starting_balance_usd=virtual_baseline_usd, marks={},
    ).synthetic_balance_usd


@dataclass(frozen=True)
class SyntheticBalance:
    """The dashboard's headline balance, derived entirely from logs we
    control (state/trades.jsonl + state/costs.jsonl + the broker's
    currently-held positions) and NOT from Alpaca account equity.
    See plans/federated-greeting-sphinx.md for why we made this the
    single source of truth.

    Formula:

        synthetic_balance_usd
          = starting_balance_usd
          + closed_gross_pnl_usd
          + open_gross_pnl_usd      # 0 when no marks
          − llm_cost_total_usd      # reset-aware
          − trading_fees_total_usd  # hybrid: real (closed) + modelled (open)

    Field conventions:
      - ``closed_gross_pnl_usd``: sum of ``(sell_price − buy_price) × qty
        × multiplier`` across FIFO-matched closes from trades.jsonl.
        Trading fees + attributed LLM costs are deducted SEPARATELY via
        the dedicated fields below; this is purely gross.
      - ``open_gross_pnl_usd``: sum of ``(mark − buy_price) × qty
        × multiplier`` across currently-open lots that have a live
        mark. Open lots without a mark contribute zero and bump
        ``unmarked_open_lots``.
      - ``llm_cost_total_usd``: ALL LLM API spend recorded in
        costs.jsonl, project-wide, reset-aware. Includes runs that
        opened no positions (e.g. all-cash decisions) — those are part
        of the experiment's true cost.
      - ``trading_fees_total_usd``: HYBRID composition (see
        ``real_trading_fees_usd`` / ``modelled_open_fees_usd``):

            trading_fees_total_usd
              = real_trading_fees_usd        # closed-side real broker fees
              + modelled_open_fees_usd       # IBKR-Pro round-trip estimate
                                             #   for currently-open positions

        Rationale: Alpaca paper reports \$0 fees on equity fills
        (``lib/trades_sync.py``), so a real-only definition would leave
        the headline showing "\$0.00 fees" while the per-position table
        prominently displays modelled fees that the Aggregate Net P&L
        already subtracts. Hybrid keeps the headline aligned with what
        the operator sees in the positions table. On a future live
        broker, ``real_trading_fees_usd`` populates from trades.jsonl
        and ``modelled_open_fees_usd`` cleanly tapers as positions
        close (modelled drops to 0 once a position is fully closed
        and its real fees land in the realised side).

      - ``real_trading_fees_usd``: sum of ``fees_usd`` across EVERY fill
        in trades.jsonl. On paper ETFs this is \$0; on options +
        live trading it populates from OCC/SEC/TAF schedules.
      - ``modelled_open_fees_usd``: sum of
        ``compute_position_pnl(...).modelled_costs_usd`` across the
        broker-held subset of positions (same source the positions
        table's per-row ``Fees`` column uses). Round-trip estimate —
        entry leg + projected exit leg.

    Known caveat (paper-only deployment safe): on a live broker the
    real entry-leg fee on a currently-open position would live in
    trades.jsonl AND ``modelled_open_fees_usd`` would also include an
    entry-leg term — slight double-count on the entry side until the
    position closes. Acceptable for the current paper-ETF deployment
    where real entry fees are \$0. Follow-up ticket should split
    ``modelled_costs_usd`` into entry / exit halves so only the exit
    leg is added here.
    """
    starting_balance_usd: float = 2500.0
    closed_gross_pnl_usd: float = 0.0
    open_gross_pnl_usd: float = 0.0
    llm_cost_total_usd: float = 0.0
    # Hybrid trading-fees total. See class docstring for the formula.
    # Two decomposed components exposed so the dashboard / Codex can
    # see real-vs-modelled at a glance without recomputing.
    trading_fees_total_usd: float = 0.0
    real_trading_fees_usd: float = 0.0
    modelled_open_fees_usd: float = 0.0
    unmarked_open_lots: int = 0
    # Sell fills that couldn't FIFO-match against an open buy lot.
    # Healthy operation never produces these (system spec is "no
    # broker shorts"). Nonzero is a data-integrity signal — the
    # synthetic balance can't account for those sells' P&L because
    # the corresponding buys are missing or out of order. Surfaced
    # for a dashboard warning rather than silently corrupting the
    # headline. Codex P1 on PR #79.
    unmatched_sell_count: int = 0

    @property
    def synthetic_balance_usd(self) -> float:
        return (
            self.starting_balance_usd
            + self.closed_gross_pnl_usd
            + self.open_gross_pnl_usd
            - self.llm_cost_total_usd
            - self.trading_fees_total_usd
        )

    @property
    def is_integrity_warning(self) -> bool:
        """True when the synthetic balance may not be trustworthy
        because the upstream trade log carries unmatched sells. The
        dashboard surfaces this as a yellow warning band so the
        operator doesn't read the headline as authoritative without
        first investigating the data anomaly."""
        return self.unmatched_sell_count > 0


def compute_synthetic_balance(
    *,
    starting_balance_usd: float = 2500.0,
    marks: dict[str, float] | None = None,
    portfolio: dict | None = None,
    broker_costs: dict[str, float] | None = None,
    held_keys: frozenset[str] | set[str] | None = None,
) -> SyntheticBalance:
    """Build a SyntheticBalance snapshot at the current moment.

    Source decomposition (each component reads its authoritative log):

    - ``closed_gross_pnl_usd`` comes from ``trades.jsonl`` via
      ``trades_pnl_view``. That log is the realized-cash source of
      truth — every closed round-trip lands there with real prices.
    - ``open_gross_pnl_usd`` comes from BROKER-HELD POSITIONS, NOT
      from open lots in trades.jsonl. The trade log can lag behind
      the broker (after a Wipe-history click, before
      ``trades_sync.sync_fills_from_alpaca`` runs, or for legacy
      pre-tracking fills), and using it for open P&L would silently
      under-report whenever the broker carries positions that aren't
      yet in the log. The positions table on the Portfolio tab reads
      the same broker source — sourcing open_gross here from the
      same place guarantees the two surfaces agree.
    - ``llm_cost_total_usd`` is the all-time, reset-aware sum from
      ``costs.jsonl``.
    - ``trading_fees_total_usd`` is HYBRID: real broker fees on closed
      trades (from trades.jsonl) PLUS modelled IBKR-Pro round-trip
      estimate on currently-open positions (from
      ``compute_position_pnl``). See ``SyntheticBalance`` docstring
      for rationale. The two components are surfaced separately on
      the dataclass as ``real_trading_fees_usd`` /
      ``modelled_open_fees_usd`` so the operator / Codex can audit.

    Args:
      ``marks``: broker-live per-symbol price map. Open lots without a
        mark contribute zero and bump ``unmarked_open_lots``.
      ``portfolio``: agent's last portfolio snapshot. When provided,
        open_gross is computed from broker-held subset of these
        positions via ``compute_portfolio_pnl``.
      ``broker_costs``: broker-reported cost basis per symbol (Alpaca's
        avg_cost). Preferred over the agent's intended ``avg_cost``
        for option positions where the agent's premium estimates can
        be 5-10× off the actual fill.
      ``held_keys``: broker's currently-held position keys (from
        ``BrokerView.held_keys``). Used to filter stale portfolio.json
        rows the broker no longer carries.

    When ``portfolio`` is None (e.g. tests, the Realized-balance card
    explicitly sourcing closed-only), open_gross falls back to the
    trades.jsonl open-lots path so the function stays callable
    without broker context.
    """
    marks = marks or {}
    view = trades_pnl_view(marks=marks)
    closed_gross = view["totals"]["realised_gross_usd"]

    open_gross = 0.0
    modelled_open_fees = 0.0  # Σ compute_position_pnl(...).modelled_costs_usd
    unmarked = 0
    if portfolio is not None:
        open_subset, _ = split_positions_by_broker_holdings(
            portfolio, held_keys=held_keys,
        )
        for p in open_subset:
            key = mark_key_for_position(p)
            mark = marks.get(key)
            if mark is None and p["kind"] == "option":
                # Option marks may be keyed by OSI in some BrokerView
                # constructions — fall back to that resolution so a
                # rename doesn't silently zero out the position.
                try:
                    osi = osi_symbol(
                        underlying=p["underlying"], expiry=p["expiry"],
                        type=p["type"], strike=p["strike"],
                    )
                    mark = marks.get(osi)
                except (ValueError, KeyError):
                    osi = None
            broker_cost_for_pos = (broker_costs or {}).get(key)
            if (
                broker_cost_for_pos is None
                and p["kind"] == "option"
                and (broker_costs or {})
            ):
                try:
                    osi = osi_symbol(
                        underlying=p["underlying"], expiry=p["expiry"],
                        type=p["type"], strike=p["strike"],
                    )
                    broker_cost_for_pos = broker_costs.get(osi)
                except (ValueError, KeyError):
                    pass
            # Modelled round-trip fee is computed even when no mark is
            # available (pnl_lib accepts current_mark_usd=None and
            # returns gross_pnl_usd=0 with modelled_costs_usd still
            # populated). Unmarked counter only tracks open_gross
            # contribution, not fees.
            breakdown = pnl_lib.compute_position_pnl(
                position=p,
                current_mark_usd=mark,
                actual_cost_per_unit=broker_cost_for_pos,
            )
            if mark is None:
                unmarked += 1
            else:
                open_gross += breakdown.gross_pnl_usd
            # Codex P1 on PR #82: only accumulate modelled fees when
            # the broker has confirmed which positions are actually
            # held. With ``held_keys=None`` (broker unreachable),
            # ``split_positions_by_broker_holdings`` returns every
            # portfolio.json row as "open" — including any that the
            # operator may have already closed manually. Charging
            # modelled fees against those phantom rows would bias
            # the synthetic balance downward during an outage. When
            # we can't verify holdings, skip the modelled-fee
            # contribution rather than risk a misleading deduction.
            if held_keys is not None:
                modelled_open_fees += float(breakdown.modelled_costs_usd)
    else:
        # Legacy fallback: derive open_gross from trades.jsonl open
        # lots. Used by tests that exercise compute_synthetic_balance
        # without a portfolio dict, and by the Realized-balance card
        # which passes marks={} so this branch contributes 0 anyway.
        # Modelled open fees stay at 0 in this branch — without a
        # portfolio we can't know which positions are currently open
        # at the broker.
        for lot in view["open"]:
            g = lot.get("gross_pnl_usd")
            if g is None:
                unmarked += 1
            else:
                open_gross += float(g)

    real_fees = total_trading_fees_usd()
    return SyntheticBalance(
        starting_balance_usd=float(starting_balance_usd),
        closed_gross_pnl_usd=float(closed_gross),
        open_gross_pnl_usd=open_gross,
        llm_cost_total_usd=total_token_cost()["cost_usd"],
        trading_fees_total_usd=real_fees + modelled_open_fees,
        real_trading_fees_usd=real_fees,
        modelled_open_fees_usd=modelled_open_fees,
        unmarked_open_lots=unmarked,
        unmatched_sell_count=int(view["totals"].get("unmatched_sell_count", 0)),
    )


def synthetic_base_usd() -> float:
    """The synthetic starting balance ($2,500 spec target), overridable via
    VIRTUAL_NAV_USD. This is the baseline both the dashboard and (from Phase 3)
    the agent's position sizing build on — never the broker's ~$100k equity."""
    import os
    raw = os.environ.get("VIRTUAL_NAV_USD")
    if not raw:
        return 2500.0
    try:
        return float(raw)
    except ValueError:
        return 2500.0


def _raw_llm_cost_total_usd() -> float:
    """Sum of ALL LLM cost rows from the raw audit log — NOT honoring the
    dashboard's display cost-reset markers. Trading/risk NAV must use this so
    that hiding costs in the UI can't silently inflate sizing capital (Codex
    P2 on PR #98)."""
    if not state.COSTS_LOG.exists():
        return 0.0
    total = 0.0
    for line in state.COSTS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            total += float(json.loads(line).get("cost_usd", 0.0) or 0.0)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return total


def _risk_nav_from(sb: "SyntheticBalance") -> float:
    """Recompute a SyntheticBalance's level using the RAW LLM cost total
    instead of the reset-aware display total. Used only by the sizing/risk
    paths (sizing NAV + the drawdown breaker), never the dashboard display."""
    return (
        sb.starting_balance_usd
        + sb.closed_gross_pnl_usd
        + sb.open_gross_pnl_usd
        - _raw_llm_cost_total_usd()
        - sb.trading_fees_total_usd
    )


def live_synthetic_nav(
    *,
    marks: dict[str, float] | None = None,
    portfolio: dict | None = None,
    broker_costs: dict[str, float] | None = None,
) -> float:
    """Mark-aware synthetic equity (RAW costs) = starting + closed + open P&L
    − raw LLM cost − fees. Used by the daily-drawdown breaker (it must see
    intraday unrealized moves and must not move on a display cost reset)."""
    marks = marks or {}
    sb = compute_synthetic_balance(
        starting_balance_usd=synthetic_base_usd(),
        marks=marks,
        portfolio=portfolio,
        broker_costs=broker_costs,
        held_keys=frozenset(marks.keys()),
    )
    return _risk_nav_from(sb)


def realized_synthetic_nav() -> float:
    """Realized-only synthetic balance the agent sizes against (Phase 3),
    using RAW LLM costs so a display cost reset can't inflate sizing capital.
    Reads logs only; no broker round trip."""
    sb = compute_synthetic_balance(starting_balance_usd=synthetic_base_usd(), marks={})
    return _risk_nav_from(sb)


def realized_balance_series(
    *, starting_balance_usd: float = 2500.0,
) -> list[dict]:
    """Time series of the realized synthetic balance for the equity curve.

    At each timestamp `t` where a close, an LLM cost, or a trading-fee
    fill landed, emit:

        synthetic_realized_balance_usd(t)
          = starting_balance_usd
          + closed_gross_pnl_usd(t)
          − llm_cost_total_usd(t)        # reset-aware
          − trading_fees_total_usd(t)

    Open-lot P&L is intentionally NOT included — the curve is
    reconstructable exactly from logs, with no need to know historical
    marks (we don't have them). The hero card is where open P&L lives.

    Each emitted point carries the component fields too, so the chart
    can expose them in hover-text for the curious operator.

    Empty when there are no closes / no LLM rows / no fees yet.
    """
    # Build per-event impact rows: (at, closed_gross_delta, llm_delta, fees_delta).
    # Closes contribute their per-trade gross + per-trade fees (both
    # sides pro-rated by compute_trades_pnl). LLM events from
    # costs.jsonl contribute as standalone cost deltas. Trade-row
    # events from trades.jsonl contribute their fees_usd at fill time.
    events: list[dict] = []
    # Closed-trade gross P&L by closed_at.
    view = trades_pnl_view(marks=None)
    for c in view["closed"]:
        events.append({
            "at": c.get("closed_at") or "",
            "closed_gross_delta": float(c.get("gross_pnl_usd") or 0.0),
            "llm_delta": 0.0,
            "fees_delta": 0.0,
        })
    # LLM cost events (reset-aware via load_costs).
    for row in load_costs(limit=10**9):
        events.append({
            "at": row.get("at") or "",
            "closed_gross_delta": 0.0,
            "llm_delta": float(row.get("cost_usd") or 0.0),
            "fees_delta": 0.0,
        })
    # Trading-fee events — each fill (buy or sell) lands its fee at
    # filled_at. NOT reset-aware (real money).
    for t in load_trades():
        fee = float(t.get("fees_usd") or 0.0)
        if fee <= 0:
            continue
        events.append({
            "at": t.get("filled_at") or "",
            "closed_gross_delta": 0.0,
            "llm_delta": 0.0,
            "fees_delta": fee,
        })
    if not events:
        return []
    # Sort chronologically; emit running totals at each tick.
    events.sort(key=lambda r: r["at"])
    out: list[dict] = []
    closed_gross = 0.0
    llm_total = 0.0
    fees_total = 0.0
    for e in events:
        closed_gross += e["closed_gross_delta"]
        llm_total += e["llm_delta"]
        fees_total += e["fees_delta"]
        out.append({
            "at": e["at"],
            "synthetic_realized_balance_usd": (
                starting_balance_usd + closed_gross - llm_total - fees_total
            ),
            "closed_gross_pnl_usd": closed_gross,
            "llm_cost_total_usd": llm_total,
            "trading_fees_total_usd": fees_total,
        })
    return out


def live_balance_tip(
    *,
    synthetic_balance: SyntheticBalance,
    series: list[dict] | None = None,
) -> dict:
    """Build a one-row "live tip" extending the realized balance
    series with the current synthetic balance (including open P&L +
    modelled open fees).

    The historical series is reconstructed exactly from
    ``trades.jsonl`` + ``costs.jsonl`` — no historical marks needed,
    so the curve is stable between cycles. But the operator wants the
    curve's tip to line up with the hero card, which DOES include
    open P&L. This helper produces that tip point: a single row at
    ``utcnow_iso()`` whose ``synthetic_balance_usd`` matches
    ``synthetic_balance.synthetic_balance_usd`` exactly.

    Returned dict shape mirrors ``realized_balance_series`` rows so
    the chart can stitch the two together cleanly, with one extra
    ``kind: "live"`` discriminator field:

        {
          "at": "<utcnow_iso>",
          "synthetic_balance_usd": <hero value>,
          "closed_gross_pnl_usd": <hero closed gross>,
          "open_gross_pnl_usd":   <hero open gross>,
          "llm_cost_total_usd":   <hero llm total>,
          "trading_fees_total_usd": <hero hybrid fees>,
          "kind": "live",
        }

    The ``series`` argument is accepted for symmetry with the chart
    code path that passes both into a renderer; this helper doesn't
    inspect it (it only needs the SyntheticBalance) but keeping the
    signature symmetric makes the call site read better.
    """
    return {
        "at": state.utcnow_iso(),
        "synthetic_balance_usd": synthetic_balance.synthetic_balance_usd,
        "closed_gross_pnl_usd": synthetic_balance.closed_gross_pnl_usd,
        "open_gross_pnl_usd": synthetic_balance.open_gross_pnl_usd,
        "llm_cost_total_usd": synthetic_balance.llm_cost_total_usd,
        "trading_fees_total_usd": synthetic_balance.trading_fees_total_usd,
        "kind": "live",
    }


def closed_trade_chips(
    *,
    marks: dict[str, float] | None = None,
    limit: int = 12,
) -> list[dict]:
    """Most-recent closed trades, shaped for the per-trade chip strip
    next to the Settled balance card.

    Each chip carries: symbol, kind, net_pnl_usd, closed_at. Sorted
    newest-first; capped at `limit` so the strip stays scannable when
    the trade log grows long.
    """
    view = trades_pnl_view(marks=marks)
    closed = view["closed"]
    closed_sorted = sorted(
        closed, key=lambda r: r.get("closed_at") or "", reverse=True,
    )
    out = []
    for r in closed_sorted[:limit]:
        out.append({
            "symbol": r["symbol"],
            "kind": r.get("kind", "etf"),
            "net_pnl_usd": r["net_pnl_usd"],
            "closed_at": r["closed_at"],
        })
    return out


def closed_trade_chips_by_ticker(
    *,
    marks: dict[str, float] | None = None,
    limit: int = 12,
) -> list[dict]:
    """All-time closed-trade contribution per ticker, shaped for the
    aggregate chip strip beneath the recent-closes strip.

    Sums ``net_pnl_usd`` across every FIFO-matched round-trip for each
    symbol so a ticker traded N times appears once with the full
    realised contribution and a ``trade_count`` of N. Sorted by
    absolute net P&L descending (biggest contributors first, positive
    or negative); tiebreak on most-recent close.
    """
    view = trades_pnl_view(marks=marks)
    closed_sorted = sorted(
        view["closed"], key=lambda r: r.get("closed_at") or "",
    )
    by_symbol: dict[str, dict] = {}
    for r in closed_sorted:
        sym = r["symbol"]
        bucket = by_symbol.setdefault(sym, {
            "symbol": sym,
            "kind": r.get("kind", "etf"),
            "net_pnl_usd": 0.0,
            "trade_count": 0,
            "last_closed_at": "",
        })
        bucket["net_pnl_usd"] += r["net_pnl_usd"]
        bucket["trade_count"] += 1
        # closed_sorted is oldest-first, so the last assignment wins.
        bucket["kind"] = r.get("kind", bucket["kind"])
        bucket["last_closed_at"] = r.get("closed_at") or bucket["last_closed_at"]
    rows = sorted(
        by_symbol.values(),
        key=lambda b: (abs(b["net_pnl_usd"]), b["last_closed_at"]),
        reverse=True,
    )
    return rows[:limit]


def cumulative_llm_cost_by_at() -> list[tuple[str, float]]:
    """Sorted (at_iso, cumulative_cost_usd) pairs across costs.jsonl,
    cost-reset-aware.

    Used to subtract cumulative LLM spend up to each NAV-history row's
    timestamp from the equity-curve Cumulative Net P&L line, so a
    reset visibly redraws the curve upward.
    """
    rows = sorted(
        ({"at": r.get("at") or "", "cost_usd": float(r.get("cost_usd", 0.0) or 0.0)}
         for r in load_costs(limit=10**9)),
        key=lambda r: r["at"],
    )
    out: list[tuple[str, float]] = []
    running = 0.0
    for r in rows:
        running += r["cost_usd"]
        out.append((r["at"], running))
    return out


def cumulative_llm_cost_at(timestamps: list[str]) -> list[float]:
    """For each timestamp, return the cumulative LLM cost as of that
    moment (post-reset). Streaming binary search via a precomputed
    sorted list of cumulative pairs — O((N+M) log N).
    """
    pairs = cumulative_llm_cost_by_at()
    if not pairs:
        return [0.0] * len(timestamps)
    ats = [p[0] for p in pairs]
    cums = [p[1] for p in pairs]
    import bisect
    out: list[float] = []
    for ts in timestamps:
        idx = bisect.bisect_right(ats, ts) - 1
        out.append(cums[idx] if idx >= 0 else 0.0)
    return out


def cost_today_usd() -> float:
    # read_costs_today already honours the daily reset marker (which is
    # stamped to the same timestamp by set_all_time_cost_reset), so an
    # all-time reset clears today's display automatically.
    return sum(r.get("cost_usd", 0.0) for r in state.read_costs_today())


def cost_for_run_usd(run_id: str) -> float:
    """Per-run display cost. Applies the all-time reset filter so a fresh
    "reset all" zeroes the in-flight run's meter too. Cap enforcement in
    lib.llm.check_caps_or_raise stays on the raw log via state.read_costs_for_run."""
    rows = state.filter_costs_post_reset(state.read_costs_for_run(run_id))
    return sum(r.get("cost_usd", 0.0) for r in rows)


def runs_count() -> int:
    """Number of orchestrator runs to date.

    Counted as distinct `run_id`s observed in `state/costs.jsonl`. Each
    orchestrator cycle invokes the LLM multiple times (one per stage, plus
    retries), so `len(load_costs())` over-counts runs by ~6-8×. Use this
    helper when you want an operator-meaningful "how many cycles has the
    system done" number.
    """
    rows = load_costs(limit=10**9)
    return len({r.get("run_id") for r in rows if r.get("run_id")})


def total_token_cost() -> dict:
    """All-time totals across this project's state/costs.jsonl. Project-scoped
    (the SDK only writes to this file from this codebase).

    NOTE: `calls` here is the number of LLM invocations (cost-log rows), NOT
    the number of orchestrator runs. Use `runs_count()` for that.
    """
    rows = load_costs(limit=10**9)
    sums = {
        "calls": len(rows),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
    }
    for r in rows:
        for k in sums:
            if k == "calls":
                continue
            sums[k] += r.get(k, 0) or 0
    sums["total_tokens"] = (
        sums["input_tokens"]
        + sums["output_tokens"]
        + sums["cache_creation_input_tokens"]
        + sums["cache_read_input_tokens"]
    )
    return sums


def cost_by_month() -> list[dict]:
    """Return sorted list of {month: 'YYYY-MM', cost_usd, total_tokens, calls}."""
    rows = load_costs(limit=10**9)
    by_month: dict[str, dict] = {}
    for r in rows:
        at = r.get("at", "")
        if len(at) < 7:
            continue
        key = at[:7]
        bucket = by_month.setdefault(
            key, {"month": key, "cost_usd": 0.0, "total_tokens": 0, "calls": 0}
        )
        bucket["calls"] += 1
        bucket["cost_usd"] += r.get("cost_usd", 0.0) or 0
        bucket["total_tokens"] += (
            (r.get("input_tokens") or 0)
            + (r.get("output_tokens") or 0)
            + (r.get("cache_creation_input_tokens") or 0)
            + (r.get("cache_read_input_tokens") or 0)
        )
    return sorted(by_month.values(), key=lambda x: x["month"])


def load_nav_history(limit: int | None = None) -> list[dict]:
    return state.read_nav_history(limit=limit)


def benchmark_view(
    starting_balance_usd: float = 2500.0,
    *,
    live_nav_usd: float | None = None,
):
    """Assemble the S&P-500-comparison MetricsBundle from local state + yfinance.

    Strategy curve source: ``realized_balance_series()`` (= starting
    balance + closed gross P&L − LLM cost − trading fees). Same series
    the Performance tab's "Synthetic balance (reconstructed)" toggle
    plots. Chosen over ``state/nav_history.jsonl`` because in the
    default ``VIRTUAL_NAV_USD=2500`` config nav_history stores the
    orchestrator's sizing notional (constant $2,500 every cycle)
    rather than actual P&L, which would make the strategy curve
    silently flat against a moving SPY (codex P2 finding).

    A baseline anchor row at the first cycle's timestamp is
    pre-pended so the equity curve starts at ``starting_balance_usd``
    on inception day even before any trade closes — the dashboard
    chart would otherwise begin abruptly at the first realised event.

    Returns None when fewer than 2 distinct trading-day points
    exist (the tab renders a friendly empty-state placeholder).
    Lets yfinance/network errors propagate so Streamlit's cache_data
    doesn't cache transient failures as missing-history.
    """
    from datetime import date as _date

    realized = realized_balance_series(starting_balance_usd=starting_balance_usd)
    # OLDEST nav_history row anchors inception. read_nav_history's
    # ``limit`` slices from the END (rows[-limit:]) so limit=1 would
    # return the LATEST cycle — which would then pre-pend the $2,500
    # baseline at today's date and let align_to_eod overwrite the real
    # realized balance for today, creating a false jump/recovery
    # (regression for codex P2).
    nav_rows = load_nav_history()
    if not realized and not nav_rows:
        return None

    inception_at = nav_rows[0]["at"] if nav_rows else realized[0]["at"]

    # Stamp the baseline at the START of inception day (00:00 UTC) so
    # any same-day realized event (e.g. first-cycle LLM cost stamped
    # mid-LLM-stage, before append_nav writes the cycle-end NAV row)
    # wins via align_to_eod's last-sample-per-day semantics — otherwise
    # day-one costs/fees get silently overwritten by the $2,500
    # baseline (regression for codex P2).
    import datetime as _dt
    _at_dt = _dt.datetime.fromisoformat(str(inception_at).replace("Z", "+00:00"))
    if _at_dt.tzinfo is None:
        _at_dt = _at_dt.replace(tzinfo=_dt.timezone.utc)
    baseline_at = (
        _at_dt.astimezone(_dt.timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    events = [{"at": baseline_at, "value": float(starting_balance_usd)}]
    for r in realized:
        events.append({
            "at": r["at"],
            "value": float(r["synthetic_realized_balance_usd"]),
        })

    from . import benchmark as bench  # local import — keeps optional deps lazy

    # Only ≥1 EOD anchor required here: build_comparison's ffill step
    # densifies a single-row strategy across the SPY trading-day index,
    # producing a flat strategy line vs a moving SPY. That's the right
    # picture for cases like an all-cash account or immediately after
    # "Reset ALL LLM costs" — the tab should render the honest "you
    # held cash while SPY moved" view instead of dying in the empty
    # state (regression for codex P2).
    eod = bench.align_to_eod(events, value_key="value")
    if len(eod) < 1:
        return None
    spy = bench.fetch_spy_total_return(eod.index[0], _date.today())
    return bench.build_comparison(
        eod,
        spy,
        starting_balance_usd,
        live_nav_usd=live_nav_usd,
        as_of=_date.today(),
    )


def load_trades() -> list[dict]:
    """Read state/trades.jsonl. Each row is one Alpaca fill with real
    fees_usd. Empty list when the log doesn't exist yet.

    No reset-marker filtering — trading fees are real, paid money; we
    don't want a display reset to make them disappear. The LLM-cost
    reset only affects token-cost rows.
    """
    return state.read_trades()


def total_trading_fees_usd() -> float:
    """Sum fees_usd across every fill in trades.jsonl. Used by the stats
    grid + the all-time totals on the Performance tab."""
    return sum(float(r.get("fees_usd", 0.0) or 0.0) for r in load_trades())


def fees_by_month() -> list[dict]:
    """Return sorted list of {month: 'YYYY-MM', fees_usd, fills}.

    Mirrors ``cost_by_month`` for trading fees: groups every fill in
    trades.jsonl by its ``filled_at`` UTC month. Each entry is a single
    bucket — both buy-side and sell-side fees count toward the same
    month they were paid in.
    """
    rows = load_trades()
    by_month: dict[str, dict] = {}
    for r in rows:
        at = r.get("filled_at") or ""
        if len(at) < 7:
            continue
        key = at[:7]
        bucket = by_month.setdefault(
            key, {"month": key, "fees_usd": 0.0, "fills": 0}
        )
        bucket["fills"] += 1
        bucket["fees_usd"] += float(r.get("fees_usd", 0.0) or 0.0)
    return sorted(by_month.values(), key=lambda x: x["month"])


def trades_pnl_view(marks: dict[str, float] | None = None) -> dict:
    """Return everything the Trades tab needs to render.

    Output keys:
      - ``closed``: list of closed-trade rows (symbol, qty, prices, gross,
        fees_usd, llm_cost_usd, net, run_id, timestamps)
      - ``open``: same shape with mark + None gross when unmarked
      - ``totals``: realised aggregates + closed/open counts

    Sources:
      - ``state/trades.jsonl`` — one row per Alpaca fill (PR #52 + #55)
      - ``state/costs.jsonl`` — LLM cost rows for equal-split attribution
      - ``marks`` — optional {symbol: per-unit mark} for unrealised PnL
        on open lots; the dashboard passes broker-live marks here.

    Honours the all-time cost reset (PR #53): costs are filtered through
    ``state.filter_costs_post_reset`` so a reset zeroes the LLM-cost
    attribution column. Trading fees are NEVER filtered — they're real
    paid money.
    """
    from . import trades as trades_lib

    trade_rows = state.read_trades()
    cost_rows = state.filter_costs_post_reset(
        [json.loads(line) for line in (
            state.COSTS_LOG.read_text(encoding="utf-8").splitlines()
            if state.COSTS_LOG.exists() else []
        ) if line.strip()]
    )
    pnl = trades_lib.compute_trades_pnl(
        trade_rows, costs=cost_rows, marks=marks or {},
    )
    return {
        "closed": [
            {
                "symbol": c.symbol,
                "kind": c.kind,
                "qty": c.qty,
                "buy_price": c.buy_price,
                "sell_price": c.sell_price,
                "opened_at": c.opened_at,
                "closed_at": c.closed_at,
                "gross_pnl_usd": c.gross_pnl_usd,
                "fees_usd": c.fees_usd,
                "llm_cost_usd": c.attributed_llm_cost_usd,
                "net_pnl_usd": c.net_pnl_usd,
                "buy_run_id": c.buy_run_id,
            }
            for c in pnl.closed
        ],
        "open": [
            {
                "symbol": o.symbol,
                "kind": o.kind,
                "qty": o.qty,
                "buy_price": o.buy_price,
                "mark": o.mark,
                "opened_at": o.opened_at,
                "gross_pnl_usd": o.gross_pnl_usd,
                "fees_usd": o.fees_usd,
                "llm_cost_usd": o.attributed_llm_cost_usd,
                "net_pnl_usd": o.net_pnl_usd,
                "buy_run_id": o.buy_run_id,
            }
            for o in pnl.open
        ],
        "totals": {
            "realised_gross_usd": pnl.total_realised_gross_usd,
            "realised_fees_usd": pnl.total_realised_fees_usd,
            "realised_llm_cost_usd": pnl.total_realised_llm_cost_usd,
            "realised_net_usd": pnl.total_realised_net_usd,
            "closed_count": len(pnl.closed),
            "open_count": len(pnl.open),
            "unmatched_sell_count": len(pnl.unmatched_sells),
        },
        # Surfaced for dashboard warnings — sells in trades.jsonl
        # that couldn't FIFO-match against an open buy lot. Healthy
        # operation never produces these; nonzero means data loss /
        # out-of-order sync / manual edit.
        "unmatched_sells": [
            {
                "symbol": u.symbol, "kind": u.kind, "qty": u.qty,
                "fill_price": u.fill_price, "filled_at": u.filled_at,
                "activity_id": u.activity_id,
            }
            for u in pnl.unmatched_sells
        ],
    }


def fees_running_total() -> list[dict]:
    """Return ``[{at, fees_usd, cum_fees_usd}]`` ordered by fill time.

    Powers the cumulative-fees line on the Performance tab. Cumulative
    sum makes it easy to spot a fee spike on a busy day vs slow drift
    from per-contract OCC fees. Returns [] when no fills.
    """
    rows = load_trades()
    out: list[dict] = []
    cum = 0.0
    # Sort by filled_at to be safe — read_trades preserves file order but
    # fills logged out of strict chronological order would distort the
    # cumulative line.
    for r in sorted(rows, key=lambda r: r.get("filled_at") or ""):
        fee = float(r.get("fees_usd", 0.0) or 0.0)
        cum += fee
        out.append({
            "at": r.get("filled_at") or "",
            "fees_usd": fee,
            "cum_fees_usd": cum,
        })
    return out


def try_load_broker_marks() -> dict[str, float]:
    """Best-effort fetch of current marks from Alpaca paper.

    Returns {} on any failure path so the dashboard renders even when:
      - alpaca-py isn't installed
      - .env doesn't have ALPACA_API_KEY / SECRET
      - the broker call errors at the network level

    The dashboard is read-only — never blocks rendering on broker issues.
    """
    try:
        from .alpaca_client import AlpacaBroker
        from .marks import marks_from_broker
        broker = AlpacaBroker()
        return marks_from_broker(broker)
    except Exception:
        return {}


def try_load_broker_marks_and_costs() -> tuple[dict[str, float], dict[str, float]]:
    """Best-effort fetch of marks AND actual cost-basis dicts from the broker.

    Returns ({}, {}) on any failure path. The two dicts share the same
    key shape (ETF symbol or synthetic `UNDERLYING|STRIKE|EXPIRY|TYPE`),
    so consumers can look up both with a single key per position.

    Use cost-basis to compute P&L that matches Alpaca's reported numbers
    — the agent's `premium_paid` in portfolio.json is an estimate that's
    often 5-10× off real option premiums.

    Kept for backwards-compatibility. New callers should prefer
    ``try_load_broker_view()`` because this 2-tuple cannot distinguish
    "broker unreachable" from "broker says zero positions" — both return
    ({}, {}). The dashboard's stale-position filter needs that distinction.
    """
    try:
        from .alpaca_client import AlpacaBroker
        from .marks import marks_from_broker, cost_basis_from_broker
        broker = AlpacaBroker()
        # Two get_positions() round-trips today; could be merged into one
        # broker call later. For a once-per-dashboard-render this is fine.
        return marks_from_broker(broker), cost_basis_from_broker(broker)
    except Exception:
        return {}, {}


@dataclass(frozen=True)
class BrokerView:
    """Snapshot of what the broker currently reports — distinguishes
    "broker unreachable" (``available=False``) from "broker says zero
    positions" (``available=True, held_keys=set()``).

    ``marks`` and ``costs`` are keyed by the same shape used throughout
    the codebase (ETF symbol or ``UNDERLYING|STRIKE|EXPIRY|TYPE``).
    ``held_keys`` is exactly ``set(costs)`` precomputed; ``cost_basis_from_broker``
    already filters qty == 0 so it's the truth about what's still open
    on the broker.

    ``nav_usd`` is the broker's live equity figure (account.equity_usd)
    captured at the moment ``try_load_broker_view`` was called — used
    by the dashboard hero so the headline number reflects realtime
    fills rather than the agent's last portfolio.json snapshot. Falls
    back to None when the account fetch fails (the hero then renders
    the portfolio.json nav_usd snapshot).

    ``captured_at`` is the wall-clock UTC ISO timestamp the snapshot
    was taken — surfaced on the hero so the operator can see at a
    glance how stale the displayed numbers are.
    """
    marks: dict[str, float]
    costs: dict[str, float]
    held_keys: frozenset[str]
    available: bool
    nav_usd: float | None = None
    captured_at: str = ""


def try_load_broker_view() -> BrokerView:
    """Best-effort fetch returning a BrokerView. On any failure path
    ``available=False`` and all dicts/sets are empty, signalling to the
    dashboard that it should NOT filter positions (since we can't tell
    if a position is stale or just temporarily unreachable).

    On success ``available=True`` and ``held_keys`` reflects what's
    currently open at the broker. Dashboards should hide portfolio.json
    rows that aren't in ``held_keys`` to avoid showing stale positions
    after a manual close / kill-condition exit / expiry.

    Codex P1 (PR #51): we must call ``broker.get_positions()`` directly
    here, NOT through ``marks_from_broker`` / ``cost_basis_from_broker``
    — those helpers swallow get_positions() exceptions and return ``{}``,
    which is indistinguishable from "broker says zero positions". If we
    routed through them, a transient broker failure would set
    ``available=True, held_keys=set()`` and the dashboard filter would
    blank the entire table. Calling get_positions() ourselves lets the
    exception bubble to the outer ``except`` and flips ``available=False``,
    so we degrade to "render everything from portfolio.json" instead.
    """
    try:
        from .alpaca_client import AlpacaBroker
        from .marks import marks_from_positions, cost_basis_from_positions
        broker = AlpacaBroker()
        positions = broker.get_positions()
    except Exception:
        return BrokerView(
            marks={}, costs={}, held_keys=frozenset(), available=False,
        )
    marks = marks_from_positions(positions)
    costs = cost_basis_from_positions(positions)
    # Live broker NAV — best-effort; if get_account fails we still
    # have the positions data. Returned as the raw broker equity for
    # the dashboard's informational sub-line. The headline balance
    # comes from compute_synthetic_balance, NOT this number.
    nav_usd: float | None = None
    try:
        nav_usd = float(broker.get_account().equity_usd)
    except Exception:
        nav_usd = None
    return BrokerView(
        marks=marks,
        costs=costs,
        held_keys=frozenset(costs),
        available=True,
        nav_usd=nav_usd,
        captured_at=state.utcnow_iso(),
    )


def mark_key_for_position(pos: dict) -> str:
    """Return the key the broker would use for this portfolio position.

    Must match lib.marks._key_for_broker_position so that membership tests
    against ``BrokerView.held_keys`` work both ways round.
    """
    if pos["kind"] == "etf":
        return pos["symbol"]
    from .marks import option_synthetic_key
    return option_synthetic_key(pos["underlying"], pos["strike"], pos["expiry"], pos["type"])


def split_positions_by_broker_holdings(
    portfolio: dict, *, held_keys: frozenset[str] | set[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Partition portfolio positions into (open_at_broker, closed_at_broker).

    When ``held_keys`` is None the broker is unreachable — everything
    stays in the open list (we can't tell what's actually held). When
    ``held_keys`` is empty the broker reachably says zero positions, so
    every portfolio entry is treated as closed.
    """
    if held_keys is None:
        return list(portfolio.get("positions", [])), []
    open_, closed = [], []
    for p in portfolio.get("positions", []):
        if mark_key_for_position(p) in held_keys:
            open_.append(p)
        else:
            closed.append(p)
    return open_, closed


def latest_run_id() -> str | None:
    if not state.DECISIONS_LOG.exists():
        return None
    rows = load_decisions(limit=10_000)
    return rows[-1]["run_id"] if rows else None


def load_run_summaries(limit: int = 20) -> list[dict]:
    """Return one human-readable summary per recent orchestrator run, newest first.

    Each summary is built from the run-dir artifacts (signals.json,
    view.json, portfolio.json, sanity.json, critique.json,
    next_run.json) + the cost log. Use this on the dashboard's
    Cycles tab — it pre-expands what would otherwise live behind
    expanders in the Decisions / Agent Logs tabs.

    Returns:
        List of dicts with keys:
          run_id, generated_at, all_cash, positions_count,
          signals_count, candidates_count, regime, sanity_status,
          critic_accept,
          construction_rationale, all_cash_rationale,
          next_run_at, next_run_rationale,
          cost_usd, cycle_intent
    """
    if not state.RUNS_DIR.exists():
        return []
    # Run dirs are named with a sortable timestamp prefix (YYYYMMDDTHHMMSSZ-xxxxxx).
    run_dirs = sorted(
        [p for p in state.RUNS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )[:limit]

    cost_by_run: dict[str, float] = {}
    for r in load_costs(limit=10**9):
        rid = r.get("run_id")
        if rid:
            cost_by_run[rid] = cost_by_run.get(rid, 0.0) + (r.get("cost_usd") or 0.0)

    # cycle_intent per run, read from decisions.jsonl (one row per stage,
    # all rows for a run carry the same intent). Default "trade" handles
    # legacy runs written before the field existed.
    intent_by_run: dict[str, str] = {}
    if state.DECISIONS_LOG.exists():
        for line in state.DECISIONS_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("run_id")
            intent = row.get("cycle_intent")
            if rid and intent and rid not in intent_by_run:
                intent_by_run[rid] = intent

    summaries: list[dict] = []
    for run_dir in run_dirs:
        rid = run_dir.name
        # Fall back to the run_id timestamp prefix when a completed
        # cycle's own artifact doesn't carry generated_at — review
        # cycles don't write portfolio.json (where trade cycles store
        # this), so without this fallback the Cycles tab renders "in
        # flight" for a successfully completed review.
        #
        # Gate on a completion marker (portfolio.json for trade OR
        # review.json for review). A run dir with neither is
        # in-flight or aborted — leave generated_at empty so the
        # Cycles tab still shows "in flight", which is a useful
        # operational signal (Codex P2 on PR #85).
        #
        # Format: YYYYMMDDTHHMMSSZ-xxxxxx.
        completed = (
            (run_dir / "portfolio.json").exists()
            or (run_dir / "review.json").exists()
        )
        rid_ts = ""
        if completed and len(rid) >= 16 and rid[8] == "T" and rid[15] == "Z":
            rid_ts = f"{rid[0:4]}-{rid[4:6]}-{rid[6:8]}T{rid[9:11]}:{rid[11:13]}:{rid[13:15]}Z"
        s: dict = {
            "run_id": rid,
            "generated_at": rid_ts,
            "all_cash": None,
            "positions_count": 0,
            "signals_count": 0,
            "candidates_count": 0,
            "regime": "",
            "sanity_status": "",
            "critic_accept": None,
            "construction_rationale": "",
            "all_cash_rationale": "",
            "next_run_at": "",
            "next_run_rationale": "",
            "cost_usd": cost_by_run.get(rid, 0.0),
            "cycle_intent": intent_by_run.get(rid, "trade"),
        }

        # portfolio.json — the headline result + rationales.
        # Defensive: artifact may be malformed (LLM output, partial writes
        # on a crashed run). Guard everything that calls len() / .startswith()
        # / treats a value as a string.
        portfolio_path = run_dir / "portfolio.json"
        if portfolio_path.exists():
            try:
                p = json.loads(portfolio_path.read_text())
                if isinstance(p, dict):
                    # Only override the rid_ts fallback when the
                    # portfolio's own generated_at is a real value —
                    # otherwise a missing/null field would wipe our
                    # fallback and re-trigger the "in flight" render.
                    portfolio_ts = p.get("generated_at") or ""
                    if portfolio_ts:
                        s["generated_at"] = portfolio_ts
                    s["all_cash"] = p.get("all_cash")
                    positions = p.get("positions")
                    s["positions_count"] = len(positions) if isinstance(positions, list) else 0
                    s["construction_rationale"] = p.get("construction_rationale", "") or ""
                    s["all_cash_rationale"] = p.get("all_cash_rationale", "") or ""
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # v2 funnel: signals → strategist candidates → portfolio positions.
        # signals.json carries the full per-ticker feature table
        # (15 in v2's curated universe); view.json carries the
        # strategist's ranked candidate list (0-6 entries).
        sig_path = run_dir / "signals.json"
        if sig_path.exists():
            try:
                sig = json.loads(sig_path.read_text())
                tickers = sig.get("tickers") if isinstance(sig, dict) else None
                s["signals_count"] = len(tickers) if isinstance(tickers, list) else 0
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        # Trade cycles write view.json; review cycles write review.json
        # (same schema). Either is acceptable for surfacing the regime +
        # candidate count on the Cycles tab.
        view_path = run_dir / "view.json"
        if not view_path.exists():
            review_path = run_dir / "review.json"
            if review_path.exists():
                view_path = review_path
        if view_path.exists():
            try:
                v = json.loads(view_path.read_text())
                if isinstance(v, dict):
                    cands = v.get("candidates")
                    s["candidates_count"] = len(cands) if isinstance(cands, list) else 0
                    s["regime"] = v.get("regime", "") or ""
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # sanity.json — overall status (pass/warn/fail) for the cycle.
        san_path = run_dir / "sanity.json"
        if san_path.exists():
            try:
                san = json.loads(san_path.read_text())
                if isinstance(san, dict):
                    s["sanity_status"] = san.get("status", "") or ""
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # critique.json — accept/reject of the constructor's first attempt.
        crit_path = run_dir / "critique.json"
        if crit_path.exists():
            try:
                crit = json.loads(crit_path.read_text())
                if isinstance(crit, dict):
                    s["critic_accept"] = crit.get("accept")
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # next_run.json — meta-scheduler's cadence call.
        # Same defensive pattern as above: any field could be null.
        nr = run_dir / "next_run.json"
        if nr.exists():
            try:
                d = json.loads(nr.read_text())
                if isinstance(d, dict):
                    s["next_run_at"] = d.get("next_run_at", "") or ""
                    s["next_run_rationale"] = d.get("rationale", "") or ""
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        summaries.append(s)

    return summaries


def option_funnel(limit: int = 20) -> list[dict]:
    """Per-cycle option funnel: surfaced → chain_ok → taken → sanity_pass → submitted.

    Diagnoses why options aren't being traded by surfacing where in the
    pipeline option candidates die. Each row corresponds to one
    orchestrator cycle (newest first) and reads the run-dir artifacts:

      - view.json / review.json — strategist's candidate list. Count
        entries with `instrument_kind in ("option_call","option_put")`.
      - chain_lookups.json — Alpaca's nearest-OTM lookup. Count entries
        whose `contract` is non-null.
      - portfolio.json — constructor's final picks. Count positions
        with `kind == "option"`.
      - sanity.json — overall status. Treated as "pass" unless the run
        failed and the failure cites an option position.
      - next_run.json["order_plan"]["results"] — actual submitted
        orders. Count results whose `symbol` is an OSI option symbol
        and status doesn't start with "error" or "skipped".

    Returns a list of dicts (newest first):
      {run_id, generated_at, surfaced, chain_ok, taken, sanity_pass,
       submitted, regime, all_cash, took_anything}

    Where `took_anything` is True if the portfolio has any position
    (ETF or option) — useful for distinguishing "all-cash cycle" from
    "took ETFs but dropped options".

    Empty list when no runs exist. Returns the most recent `limit`
    cycles, mirroring load_run_summaries.
    """
    if not state.RUNS_DIR.exists():
        return []
    run_dirs = sorted(
        [p for p in state.RUNS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )[:limit]

    out: list[dict] = []
    for run_dir in run_dirs:
        rid = run_dir.name
        row: dict = {
            "run_id": rid,
            "generated_at": "",
            "surfaced": 0,
            "chain_ok": 0,
            "taken": 0,
            "sanity_pass": None,
            "submitted": 0,
            "regime": "",
            "all_cash": None,
            "took_anything": False,
        }
        # generated_at from rid prefix as a fallback.
        if len(rid) >= 16 and rid[8] == "T" and rid[15] == "Z":
            row["generated_at"] = (
                f"{rid[0:4]}-{rid[4:6]}-{rid[6:8]}T"
                f"{rid[9:11]}:{rid[11:13]}:{rid[13:15]}Z"
            )

        # view.json — option candidates surfaced.
        view_path = run_dir / "view.json"
        if not view_path.exists():
            review_path = run_dir / "review.json"
            if review_path.exists():
                view_path = review_path
        if view_path.exists():
            try:
                v = json.loads(view_path.read_text())
                if isinstance(v, dict):
                    row["regime"] = v.get("regime", "") or ""
                    cands = v.get("candidates") or []
                    row["surfaced"] = sum(
                        1 for c in cands
                        if isinstance(c, dict) and c.get("instrument_kind") in (
                            "option_call", "option_put",
                        )
                    )
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # chain_lookups.json — how many candidates Alpaca could resolve
        # to a real OTM contract.
        cl_path = run_dir / "chain_lookups.json"
        if cl_path.exists():
            try:
                cl = json.loads(cl_path.read_text())
                if isinstance(cl, dict):
                    lookups = cl.get("lookups") or []
                    row["chain_ok"] = sum(
                        1 for l in lookups
                        if isinstance(l, dict) and l.get("contract") is not None
                    )
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # portfolio.json — option positions the constructor took.
        port_path = run_dir / "portfolio.json"
        if port_path.exists():
            try:
                p = json.loads(port_path.read_text())
                if isinstance(p, dict):
                    row["all_cash"] = p.get("all_cash")
                    positions = p.get("positions") or []
                    row["took_anything"] = len(positions) > 0
                    row["taken"] = sum(
                        1 for pos in positions
                        if isinstance(pos, dict) and pos.get("kind") == "option"
                    )
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # sanity.json — pass/warn/fail. Only meaningful when the
        # constructor took options.
        if row["taken"] > 0:
            san_path = run_dir / "sanity.json"
            if san_path.exists():
                try:
                    san = json.loads(san_path.read_text())
                    if isinstance(san, dict):
                        status = (san.get("status") or "").lower()
                        # pass and warn both let the run continue; only
                        # fail (with SANITY_BLOCK_ON_FAIL=true) stops it.
                        row["sanity_pass"] = status in ("pass", "warn", "ok")
                except (json.JSONDecodeError, OSError, TypeError):
                    pass
            else:
                row["sanity_pass"] = None

        # next_run.json["order_plan"]["results"] — submitted orders.
        # OSI option symbols match _OSI_RE (6-letter underlying + YYMMDD
        # + C|P + 8-digit strike). Skip "error..." and "skipped..."
        # statuses since those didn't actually submit.
        nr_path = run_dir / "next_run.json"
        if nr_path.exists():
            try:
                nr = json.loads(nr_path.read_text())
                if isinstance(nr, dict):
                    plan = nr.get("order_plan") or {}
                    results = plan.get("results") or []
                    # Lazy import to avoid pulling lib.orders' side effects.
                    from . import orders as orders_lib
                    row["submitted"] = sum(
                        1 for r in results
                        if isinstance(r, dict)
                        and orders_lib.is_osi_symbol(r.get("symbol") or "")
                        and r.get("side") == "buy"
                        and not (r.get("status") or "").startswith(("error", "skipped"))
                    )
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        out.append(row)
    return out


def _bias_for_position(pos: dict) -> str:
    """Bull / Bear / — classification for the positions-table Bias column.

    The system is long-only, but a bear thesis is expressed via either a
    long inverse-leveraged ETF (SQQQ, SPXU, TZA, SOXS, FAZ, DUST) or a
    long put. Surfacing the direction at a glance saves the reader from
    decoding the leverage_factor sign and the option type by hand.

    Returns 'Bull' / 'Bear' / '—' (the dash for cases we can't classify
    cleanly, e.g. UVXY/BITX which are bullish on vol or crypto but don't
    map cleanly onto an equity bull/bear axis).
    """
    if pos["kind"] == "etf":
        entry = universe_lib.by_symbol(pos["symbol"])
        if entry is None:
            return "—"
        # UVXY (vol) and BITX (crypto) carry positive leverage but are not
        # equity-bull instruments — show their own labels so the reader
        # isn't misled into thinking UVXY long = bullish equities.
        if pos["symbol"] == "UVXY":
            return "Long vol"
        if pos["symbol"] == "BITX":
            return "Long crypto"
        if entry.leverage_factor > 0:
            return "Bull"
        if entry.leverage_factor < 0:
            return "Bear"
        return "—"
    # Options: call = bullish on the underlying, put = bearish.
    return "Bull" if pos["type"] == "call" else "Bear"


def _opened_at_map_from_trades(trade_rows: list[dict]) -> dict[str, str]:
    """Open timestamp of the CURRENT position instance per symbol.

    Used to derive "Days held" on the positions table. Anchored on the
    earliest *currently-open* buy lot (FIFO) — NOT the earliest buy fill
    ever seen for the symbol. This matters when a position is fully closed
    and later reopened: the old lots are consumed by the closing sell, so
    the reopened position gets a fresh anchor (days-held resets) instead of
    inheriting the original instance's age. A partial close / averaging-in
    keeps the original anchor, because the position stayed continuously open.

    Reuses the FIFO matcher in ``lib.trades.compute_trades_pnl`` — the same
    engine that drives realised/unrealised PnL — so days-held and PnL agree
    on what "the current open instance" is. Keyed by the broker symbol (ETF
    ticker, OSI for options), the same convention the positions table uses
    for its `costs`/`marks` lookups.

    Defensive: any failure (malformed log, out-of-order rows) falls back to
    an empty map so the positions table renders "—" rather than breaking.
    """
    from . import trades as trades_lib
    try:
        pnl = trades_lib.compute_trades_pnl(trade_rows)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for lot in pnl.open:
        sym = lot.symbol
        opened_at = lot.opened_at or ""
        if not sym or not opened_at:
            continue
        prev = out.get(sym)
        if prev is None or opened_at < prev:
            out[sym] = opened_at
    return out


def _days_held(opened_at: str | None) -> int | None:
    """Whole-days elapsed since `opened_at` (UTC). None when missing/bad."""
    if not opened_at:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, int(delta.total_seconds() // 86400))
    except (ValueError, TypeError):
        return None


def _kill_summary(kill: dict) -> str:
    """Compact one-cell summary of kill_conditions for the positions table.

    Folds the underlying-price guard and the time stop into a single
    string alongside the max-loss percentage. Empty fields are dropped so
    a position with only `max_loss_pct` set still renders cleanly.
    """
    parts: list[str] = [f"≤{kill.get('max_loss_pct', 0):g}% loss"]
    below = kill.get("underlying_price_below")
    above = kill.get("underlying_price_above")
    if below is not None:
        parts.append(f"≤${below:g}")
    if above is not None:
        parts.append(f"≥${above:g}")
    time_stop = kill.get("time_stop_utc")
    if time_stop:
        # Strip the time portion — the date alone is enough for a glance.
        parts.append(f"by {time_stop[:10]}")
    return " · ".join(parts)


def position_table_rows(
    portfolio: dict,
    marks: dict[str, float] | None = None,
    costs: dict[str, float] | None = None,
    held_keys: frozenset[str] | set[str] | None = None,
    opened_at_by_symbol: dict[str, str] | None = None,
) -> list[dict]:
    """Flatten ETF + option rows into uniform columns for st.dataframe.

    `marks` and `costs` are both keyed by the same convention (ETF symbol,
    or `f"{underlying}|{strike}|{expiry}|{type}"` for options — same shape
    used by monitor.py / compute_portfolio_pnl / marks_from_broker /
    cost_basis_from_broker).

    Per-row precedence:
      - **Cost / Notional**: prefer broker's actual `avg_cost` (`costs`)
        when present, otherwise fall back to the agent's `avg_cost` /
        `premium_paid` from portfolio.json. This matters for options
        because the agent's premium estimates are often 5-10× off real
        market premiums — the broker fill is the truth.
      - **Mark / P&L**: same — prefer live broker mark, fall back to
        portfolio.json values.
      - When `costs` provides a real fill price, P&L is computed against
        THAT, not the agent's intended premium. Otherwise we'd show a
        fictional -$290 loss on a position that's actually -$2.
    """
    marks = marks or {}
    costs = costs or {}
    opened_at_by_symbol = opened_at_by_symbol or {}
    out: list[dict] = []
    for p in portfolio.get("positions", []):
        # Stale-position filter: when the broker is reachable and reports
        # which keys it still holds, hide portfolio.json rows the broker
        # no longer carries (manual close, kill-condition exit, expiry).
        # held_keys=None means "broker unreachable, don't filter" — we
        # render everything in that case rather than blank the dashboard.
        if held_keys is not None and mark_key_for_position(p) not in held_keys:
            continue
        bias = _bias_for_position(p)
        kill_cell = _kill_summary(p["kill_conditions"])
        if p["kind"] == "etf":
            key = p["symbol"]
            mark = marks.get(key)
            broker_cost = costs.get(key)
            cost_per_unit = broker_cost if broker_cost is not None else p["avg_cost"]
            shares = p["shares"]
            opened_at = opened_at_by_symbol.get(key)
            row = {
                "Symbol": p["symbol"],
                "Kind": "ETF",
                "Bias": bias,
                "Leverage": f"{p.get('leverage_factor', 1):g}x",
                "DTE": "—",
                "Qty": shares,
                "Entry": cost_per_unit,
                "Notional": shares * cost_per_unit,
                "% NAV": p["position_pct"],
                "Days held": _days_held(opened_at),
                "Greeks": "—",
                "Kill": kill_cell,
            }
        else:
            contracts = p["contracts"]
            g = p["greeks"]
            # Look up via the canonical synthetic key (strike-normalised so an
            # integer JSON strike matches the broker's float); fall back to OSI.
            from .marks import option_synthetic_key
            synth_key = option_synthetic_key(p["underlying"], p["strike"], p["expiry"], p["type"])
            mark = marks.get(synth_key)
            broker_cost = costs.get(synth_key)
            opened_at = None
            try:
                osi = osi_symbol(
                    underlying=p["underlying"], expiry=p["expiry"],
                    type=p["type"], strike=p["strike"],
                )
            except (ValueError, KeyError):
                osi = None
            if mark is None and osi is not None:
                mark = marks.get(osi)
            if broker_cost is None and osi is not None:
                broker_cost = costs.get(osi)
            # Trade history stores option symbols as OSI; that's our only
            # lookup key for the opened-at map.
            if osi is not None:
                opened_at = opened_at_by_symbol.get(osi)
            cost_per_unit = broker_cost if broker_cost is not None else p["premium_paid"]
            premium_usd = cost_per_unit * 100 * contracts
            row = {
                "Symbol": f"{p['underlying']} {p['type'].upper()} {p['strike']} {p['expiry']}",
                "Kind": "OPT",
                "Bias": bias,
                "Leverage": "—",
                "DTE": p.get("dte", "—"),
                "Qty": contracts,
                "Entry": cost_per_unit,
                "Notional": premium_usd,
                "% NAV": p["position_pct"],
                "Days held": _days_held(opened_at),
                "Greeks": (
                    f"Δ{g['delta']:.2f} Θ{g['theta']:.2f} "
                    f"IV {g['iv']*100:.0f}% (p{int(g['iv_percentile'])})"
                ),
                "Kill": kill_cell,
            }
        # Pass the broker-truth cost basis into the P&L helper so option
        # P&L reflects actual fill, not the agent's premium estimate.
        breakdown = pnl_lib.compute_position_pnl(
            position=p,
            current_mark_usd=mark,
            actual_cost_per_unit=cost_per_unit,
        )
        row["Mark"] = mark if mark is not None else None
        # Δ% (move since entry, gross) — surfaces the percent move
        # independent of position size, complementing the dollar P&L
        # columns. Computed off the per-unit prices so it works for both
        # ETF shares and option per-contract premiums.
        if mark is not None and cost_per_unit:
            row["Δ%"] = (mark - cost_per_unit) / cost_per_unit * 100.0
        else:
            row["Δ%"] = None
        # Modelled trading costs for THIS position — round-trip
        # estimate (entry leg + projected close): spread + commission
        # + reg fees. Mirrors what the Performance tab "Modelled
        # trading costs" aggregate is built from. Makes the per-row
        # Net P&L breakdown self-explanatory (Gross − Fees = Net).
        row["Fees"] = breakdown.modelled_costs_usd
        row["Gross P&L"] = breakdown.gross_pnl_usd if mark is not None else None
        row["Net P&L"] = breakdown.net_pnl_usd if mark is not None else None
        out.append(row)
    return out


def allocation_pie(portfolio: dict) -> list[dict]:
    rows = []
    for p in portfolio.get("positions", []):
        symbol = p["symbol"] if p["kind"] == "etf" else f"{p['underlying']} {p['type']}"
        rows.append({"label": symbol, "value": p["position_pct"]})
    cash_pct = max(0.0, 100.0 - sum(r["value"] for r in rows))
    if cash_pct > 0:
        rows.append({"label": "Cash", "value": cash_pct})
    return rows
