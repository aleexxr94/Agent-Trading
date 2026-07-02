"""Pure data-layer helpers for the dashboard.

Separated from dashboard.py so they can be unit-tested without streamlit
installed. Keep this file streamlit-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import alpaca_costs
from . import pnl as pnl_lib
from . import state
from . import universe as universe_lib

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
        elif src == "live":
            # Live-era rows are real-equity units already; the legacy paper
            # anchor offset must never be subtracted from them.
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
      - ``closed_gross_pnl_usd``: sum of ``(sell_price − buy_price) × qty``
        across FIFO-matched closes from trades.jsonl.
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
              + modelled_open_fees_usd       # modelled round-trip estimate
                                             #   (conservative retail friction)
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
        in trades.jsonl. On paper ETFs this is \$0; on live trading it
        populates from the SEC/TAF schedules.
      - ``modelled_open_fees_usd``: sum of
        the projected EXIT-leg regulatory fees over the broker-held
        subset of positions. The entry leg is NOT added here — it
        already lands in trades.jsonl at fill time (counted via
        ``real_trading_fees_usd`` / ``realized_slippage_usd``), so
        charging a round-trip would double-count the entry. This is the
        entry/exit split that the earlier "known caveat" called for.
        Slippage is tracked separately (see the slippage fields).
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
    # Modelled slippage/spread cost — the dominant live friction that
    # Alpaca paper (and even live) never reports. ``realized_slippage_usd``
    # is the sum of per-fill ``slippage_usd`` already in trades.jsonl
    # (closed round-trips + the entry leg of still-open positions);
    # ``modelled_open_slippage_usd`` is the projected EXIT-leg slippage on
    # currently-open positions (entry leg is already counted in the realized
    # term, so we add only the exit to avoid double-counting). Both feed
    # ``slippage_total_usd``, subtracted in the headline alongside fees.
    realized_slippage_usd: float = 0.0
    modelled_open_slippage_usd: float = 0.0
    slippage_total_usd: float = 0.0
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
            - self.slippage_total_usd
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
      trades (from trades.jsonl) PLUS modelled round-trip estimate
      (conservative retail friction) on currently-open positions (from
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
        when computing P&L against the real fill.
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
    modelled_open_fees = 0.0       # Σ projected EXIT-leg fees (commission+reg)
    modelled_open_slippage = 0.0   # Σ projected EXIT-leg slippage
    unmarked = 0
    # Project open-position costs only when the cost model is active (same
    # gate the fill injector uses, so PAPER_COST_MODEL=false / live disables
    # both consistently — otherwise the NAV would be cost-netted while the
    # fill log is gross).
    project_costs = alpaca_costs.cost_model_active()
    # Per-symbol open qty already recorded in trades.jsonl (its entry-leg cost
    # is therefore in realized_slippage_usd). For broker qty in excess of this
    # (an unsynced open or add: sync lag / post-wipe / legacy lot), the entry
    # leg is missing, so we add it below. Self-corrects once the fill syncs.
    logged_open_qty: dict[str, float] = {}
    for o in view["open"]:
        logged_open_qty[o["symbol"]] = logged_open_qty.get(o["symbol"], 0.0) + float(o["qty"])
    if portfolio is not None:
        open_subset, _ = split_positions_by_broker_holdings(
            portfolio, held_keys=held_keys,
        )
        for p in open_subset:
            key = mark_key_for_position(p)
            mark = marks.get(key)
            broker_cost_for_pos = (broker_costs or {}).get(key)
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
            # Codex P1 on PR #82: only accumulate modelled costs when
            # the broker has confirmed which positions are actually
            # held. With ``held_keys=None`` (broker unreachable),
            # ``split_positions_by_broker_holdings`` returns every
            # portfolio.json row as "open" — including any that the
            # operator may have already closed manually. Charging
            # modelled costs against those phantom rows would bias
            # the synthetic balance downward during an outage. When
            # we can't verify holdings, skip the contribution.
            #
            # We add only the projected EXIT leg: the entry leg's cost
            # already lands in trades.jsonl at fill time (real_fees +
            # realized_slippage below), so charging a full round-trip
            # here would double-count the entry.
            if held_keys is not None and project_costs:
                cost = pnl_lib.model_position_cost(p)
                modelled_open_fees += float(cost.commission_usd + cost.reg_fees_usd)
                # Exit-leg slippage on the full broker position, plus entry-leg
                # slippage on any shares not yet logged in trades.jsonl (an
                # unsynced open or add — its entry cost isn't in
                # realized_slippage_usd yet). Match by quantity, not just symbol
                # presence, so a partial add to an already-logged symbol is
                # handled. Self-corrects as fills sync.
                exit_slip = float(cost.half_spread_usd)
                broker_qty = float(p.get("shares") or 0.0)
                unlogged_qty = max(0.0, broker_qty - logged_open_qty.get(p.get("symbol"), 0.0))
                per_share_slip = (exit_slip / broker_qty) if broker_qty > 0 else 0.0
                modelled_open_slippage += exit_slip + per_share_slip * unlogged_qty
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
    realized_slippage = total_slippage_usd()
    slippage_total = realized_slippage + modelled_open_slippage
    return SyntheticBalance(
        starting_balance_usd=float(starting_balance_usd),
        closed_gross_pnl_usd=float(closed_gross),
        open_gross_pnl_usd=open_gross,
        llm_cost_total_usd=total_token_cost()["cost_usd"],
        trading_fees_total_usd=real_fees + modelled_open_fees,
        real_trading_fees_usd=real_fees,
        modelled_open_fees_usd=modelled_open_fees,
        realized_slippage_usd=realized_slippage,
        modelled_open_slippage_usd=modelled_open_slippage,
        slippage_total_usd=slippage_total,
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
        - sb.slippage_total_usd
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
          − trading_fees_total_usd(t)    # regulatory (real/modelled)
          − slippage_total_usd(t)        # modelled spread cost

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
            "slippage_delta": 0.0,
        })
    # LLM cost events (reset-aware via load_costs).
    for row in load_costs(limit=10**9):
        events.append({
            "at": row.get("at") or "",
            "closed_gross_delta": 0.0,
            "llm_delta": float(row.get("cost_usd") or 0.0),
            "fees_delta": 0.0,
            "slippage_delta": 0.0,
        })
    # Trading-cost events — each fill (buy or sell) lands its fee +
    # modelled slippage at filled_at. NOT reset-aware (real/modelled cost).
    for t in load_trades():
        fee = float(t.get("fees_usd") or 0.0)
        slip = float(t.get("slippage_usd") or 0.0)
        if fee <= 0 and slip <= 0:
            continue
        events.append({
            "at": t.get("filled_at") or "",
            "closed_gross_delta": 0.0,
            "llm_delta": 0.0,
            "fees_delta": fee,
            "slippage_delta": slip,
        })
    if not events:
        return []
    # Sort chronologically; emit running totals at each tick.
    events.sort(key=lambda r: r["at"])
    out: list[dict] = []
    closed_gross = 0.0
    llm_total = 0.0
    fees_total = 0.0
    slippage_total = 0.0
    for e in events:
        closed_gross += e["closed_gross_delta"]
        llm_total += e["llm_delta"]
        fees_total += e["fees_delta"]
        slippage_total += e["slippage_delta"]
        out.append({
            "at": e["at"],
            "synthetic_realized_balance_usd": (
                starting_balance_usd + closed_gross
                - llm_total - fees_total - slippage_total
            ),
            "closed_gross_pnl_usd": closed_gross,
            "llm_cost_total_usd": llm_total,
            "trading_fees_total_usd": fees_total,
            "slippage_total_usd": slippage_total,
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
        "slippage_total_usd": synthetic_balance.slippage_total_usd,
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


def cost_by_stage() -> list[dict]:
    """Per-pipeline-stage LLM cost rollup from state/costs.jsonl.

    Returns ``[{stage, calls, cost_usd, total_tokens, cache_hit_pct}]``
    sorted by cost descending. ``cache_hit_pct`` is
    ``100 * cache_read / (input + cache_creation + cache_read)`` over the
    stage's summed token counts (0.0 when the denominator is 0).
    Reset-aware via load_costs(); [] when no cost history exists.
    """
    rows = load_costs(limit=10**9)
    by_stage: dict[str, dict] = {}
    for r in rows:
        stage = str(r.get("stage") or "unknown")
        b = by_stage.setdefault(stage, {
            "stage": stage, "calls": 0, "cost_usd": 0.0,
            "_input": 0, "_creation": 0, "_read": 0, "_output": 0,
        })
        b["calls"] += 1
        b["cost_usd"] += r.get("cost_usd", 0.0) or 0
        b["_input"] += r.get("input_tokens") or 0
        b["_creation"] += r.get("cache_creation_input_tokens") or 0
        b["_read"] += r.get("cache_read_input_tokens") or 0
        b["_output"] += r.get("output_tokens") or 0
    out = []
    for b in by_stage.values():
        denom = b["_input"] + b["_creation"] + b["_read"]
        out.append({
            "stage": b["stage"],
            "calls": b["calls"],
            "cost_usd": b["cost_usd"],
            "total_tokens": denom + b["_output"],
            "cache_hit_pct": (100.0 * b["_read"] / denom) if denom > 0 else 0.0,
        })
    return sorted(out, key=lambda x: x["cost_usd"], reverse=True)


def cache_hit_trend(limit: int = 200) -> list[dict]:
    """Per-run prompt-cache hit rate from state/costs.jsonl.

    ``100 * cache_read / (input + cache_creation + cache_read)`` over
    each run's summed token counters — same definition as
    ``cost_by_stage``. Sourced from cost rows, NOT the decision log:
    the orchestrator writes every decision row with a hard-coded
    ``prompt_cache_hit_pct: 0.0``, while lib/llm.py records the real
    cache token counters in costs.jsonl (codex P2 on PR #108).

    Returns ``[{run_id, at, cache_hit_pct}]`` oldest→newest, one row
    per run. Runs whose cost rows carry no token counters are skipped.
    ``at`` is the run's earliest cost-row timestamp. Reset-aware via
    load_costs().
    """
    rows = load_costs(limit=10**9)
    by_run: dict[str, dict] = {}
    for r in rows:
        rid = str(r.get("run_id") or "")
        if not rid:
            continue
        b = by_run.setdefault(rid, {
            "run_id": rid, "at": "", "_input": 0, "_creation": 0, "_read": 0,
        })
        b["_input"] += r.get("input_tokens") or 0
        b["_creation"] += r.get("cache_creation_input_tokens") or 0
        b["_read"] += r.get("cache_read_input_tokens") or 0
        at = str(r.get("at") or "")
        if at and (not b["at"] or at < b["at"]):
            b["at"] = at
    out = []
    for b in by_run.values():
        denom = b["_input"] + b["_creation"] + b["_read"]
        if denom <= 0:
            continue
        out.append({
            "run_id": b["run_id"],
            "at": b["at"],
            "cache_hit_pct": 100.0 * b["_read"] / denom,
        })
    # run_ids are timestamp-prefixed, so sorting by run_id is chronological.
    out.sort(key=lambda x: x["run_id"])
    return out[-limit:]


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


def total_slippage_usd() -> float:
    """Sum slippage_usd across every fill in trades.jsonl. Modelled spread
    cost (lib.alpaca_costs) — the dominant live friction, never reported by
    Alpaca even on a funded account. $0 on legacy rows that predate the cost
    model."""
    return sum(float(r.get("slippage_usd", 0.0) or 0.0) for r in load_trades())


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
                "slippage_usd": c.slippage_usd,
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
                "slippage_usd": o.slippage_usd,
                "llm_cost_usd": o.attributed_llm_cost_usd,
                "net_pnl_usd": o.net_pnl_usd,
                "buy_run_id": o.buy_run_id,
            }
            for o in pnl.open
        ],
        "totals": {
            "realised_gross_usd": pnl.total_realised_gross_usd,
            "realised_fees_usd": pnl.total_realised_fees_usd,
            "realised_slippage_usd": pnl.total_realised_slippage_usd,
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


def trade_stats(closed: list[dict]) -> dict | None:
    """Aggregate statistics over closed-trade rows from
    ``trades_pnl_view()["closed"]``.

    Win/loss classification uses NET P&L (gross − fees − attributed LLM
    cost), consistent with the Trades tab's "Realised net" headline; a
    $0.00 net trade counts as a non-win. Returns None when there are no
    closed trades. Keys:
      - ``win_rate_pct``, ``wins``, ``losses``
      - ``profit_factor`` — gross win sum / |loss sum|; None when no
        losing trades exist (undefined, not infinite-good)
      - ``avg_win_usd`` / ``avg_loss_usd`` — None when the side is empty
      - ``avg_hold_hours`` — None when no row has parseable
        ``opened_at``/``closed_at`` timestamps
      - ``best`` / ``worst`` — ``{symbol, net_pnl_usd}`` dicts
    """
    from . import trades as trades_lib

    rows = [
        r for r in (closed or [])
        if isinstance(r.get("net_pnl_usd"), (int, float))
    ]
    if not rows:
        return None

    nets = [float(r["net_pnl_usd"]) for r in rows]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]

    profit_factor: float | None = None
    if losses:
        profit_factor = sum(wins) / abs(sum(losses))

    hold_hours: list[float] = []
    for r in rows:
        opened = trades_lib._parse_iso_utc(r.get("opened_at"))
        closed_dt = trades_lib._parse_iso_utc(r.get("closed_at"))
        if opened is None or closed_dt is None or closed_dt < opened:
            continue
        hold_hours.append((closed_dt - opened).total_seconds() / 3600.0)

    best_row = max(rows, key=lambda r: float(r["net_pnl_usd"]))
    worst_row = min(rows, key=lambda r: float(r["net_pnl_usd"]))

    return {
        "win_rate_pct": 100.0 * len(wins) / len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": profit_factor,
        "avg_win_usd": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_usd": (sum(losses) / len(losses)) if losses else None,
        "avg_hold_hours": (sum(hold_hours) / len(hold_hours)) if hold_hours else None,
        "best": {
            "symbol": best_row.get("symbol", "?"),
            "net_pnl_usd": float(best_row["net_pnl_usd"]),
        },
        "worst": {
            "symbol": worst_row.get("symbol", "?"),
            "net_pnl_usd": float(worst_row["net_pnl_usd"]),
        },
    }


def fees_running_total() -> list[dict]:
    """Return ``[{at, fees_usd, cum_fees_usd}]`` ordered by fill time.

    Powers the cumulative-fees line on the Performance tab. Cumulative
    sum makes it easy to spot a fee spike on a busy day vs slow drift
    from per-share regulatory fees. Returns [] when no fills.
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
    key shape (ETF symbol), so consumers can look up both with a single
    key per position.

    Use cost-basis to compute P&L that matches Alpaca's reported numbers
    — the broker's reported fill is the source of truth.

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

    ``marks`` and ``costs`` are keyed by ETF symbol (the shape used
    throughout the codebase).
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
    after a manual close or kill-condition exit.

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
    against ``BrokerView.held_keys`` work both ways round. ETF-only system:
    the key is the position symbol.
    """
    return pos["symbol"]


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


def _bias_for_position(pos: dict) -> str:
    """Bull / Bear / — classification for the positions-table Bias column.

    The system is long-only and ETF-only: a bearish thesis is expressed by
    holding a long inverse-leveraged ETF (SQQQ, SPXU, TZA, SOXS, FAZ, DUST,
    …), never a short or a put. Surfacing the direction at a glance saves
    the reader from decoding the leverage_factor sign by hand.

    Returns 'Bull' / 'Bear' / '—' (the dash for cases we can't classify
    cleanly, e.g. UVXY/BITX/BITI which map onto vol or crypto rather than
    an equity bull/bear axis — they get their own labels).
    """
    entry = universe_lib.by_symbol(pos["symbol"])
    if entry is None:
        return "—"
    # Vol / crypto carry their own labels so the reader isn't misled into
    # reading UVXY long as bullish equities.
    if pos["symbol"] == "UVXY":
        return "Long vol"
    if pos["symbol"] == "BITX":
        return "Long crypto"
    if pos["symbol"] == "BITI":
        return "Short crypto"
    if entry.leverage_factor > 0:
        return "Bull"
    if entry.leverage_factor < 0:
        return "Bear"
    return "—"


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
    ticker), the same convention the positions table uses for its
    `costs`/`marks` lookups.

    Defensive: any failure (malformed log, out-of-order rows) falls back to
    an empty map so the positions table renders "—" rather than breaking.
    Rows are sorted by ``filled_at`` first — the log is append-order, and an
    out-of-order sell ahead of its buy would otherwise leave a phantom open
    lot (mirrors the sorting other dashboard trade-log readers already do).
    """
    from . import trades as trades_lib
    try:
        rows = sorted(trade_rows, key=lambda r: r.get("filled_at") or "")
        pnl = trades_lib.compute_trades_pnl(rows)
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
    """Flatten ETF position rows into uniform columns for st.dataframe.

    `marks` and `costs` are both keyed by ETF symbol — the same shape used
    by monitor.py / compute_portfolio_pnl / marks_from_broker /
    cost_basis_from_broker.

    Per-row precedence:
      - **Cost / Notional**: prefer broker's actual `avg_cost` (`costs`)
        when present, otherwise fall back to the agent's `avg_cost` from
        portfolio.json — the broker fill is the truth.
      - **Mark / P&L**: same — prefer live broker mark, fall back to
        portfolio.json values.
    """
    marks = marks or {}
    costs = costs or {}
    opened_at_by_symbol = opened_at_by_symbol or {}
    out: list[dict] = []
    for p in portfolio.get("positions", []):
        # Stale-position filter: when the broker is reachable and reports
        # which keys it still holds, hide portfolio.json rows the broker
        # no longer carries (manual close or kill-condition exit).
        # held_keys=None means "broker unreachable, don't filter" — we
        # render everything in that case rather than blank the dashboard.
        if held_keys is not None and mark_key_for_position(p) not in held_keys:
            continue
        bias = _bias_for_position(p)
        kill_cell = _kill_summary(p["kill_conditions"])
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
            "Qty": shares,
            "Entry": cost_per_unit,
            "Notional": shares * cost_per_unit,
            "% NAV": p["position_pct"],
            "Days held": _days_held(opened_at),
            "Kill": kill_cell,
        }
        # Pass the broker-truth cost basis into the P&L helper so P&L
        # reflects the actual fill, not the agent's estimate.
        breakdown = pnl_lib.compute_position_pnl(
            position=p,
            current_mark_usd=mark,
            actual_cost_per_unit=cost_per_unit,
        )
        row["Mark"] = mark if mark is not None else None
        # Δ% (move since entry, gross) — surfaces the percent move
        # independent of position size, complementing the dollar P&L
        # columns. Computed off the per-share prices.
        if mark is not None and cost_per_unit:
            row["Δ%"] = (mark - cost_per_unit) / cost_per_unit * 100.0
        else:
            row["Δ%"] = None
        # Modelled trading costs for THIS position — round-trip
        # estimate (entry leg + projected close): spread + commission
        # + reg fees. Mirrors what the Performance tab "Modelled
        # trading costs" aggregate is built from. Makes the per-row
        # Net P&L breakdown self-explanatory (Gross − Fees = Net).
        # Gated on the same cost-model switch as every other surface:
        # when disabled (PAPER_COST_MODEL=false / live), show gross so the
        # table doesn't keep netting costs after the operator turned them off.
        row["Gross P&L"] = breakdown.gross_pnl_usd if mark is not None else None
        if alpaca_costs.cost_model_active():
            row["Fees"] = breakdown.modelled_costs_usd
            row["Net P&L"] = breakdown.net_pnl_usd if mark is not None else None
        else:
            row["Fees"] = 0.0
            row["Net P&L"] = breakdown.gross_pnl_usd if mark is not None else None
        out.append(row)
    return out


def allocation_pie(portfolio: dict) -> list[dict]:
    rows = []
    for p in portfolio.get("positions", []):
        rows.append({"label": p["symbol"], "value": p["position_pct"]})
    cash_pct = max(0.0, 100.0 - sum(r["value"] for r in rows))
    if cash_pct > 0:
        rows.append({"label": "Cash", "value": cash_pct})
    return rows


# ---------- Portfolio tab: universe reference table ----------

# Presentation-only factor metadata for the dashboard's universe reference
# table. Deliberately NOT part of universe.metadata_block() — that block
# feeds signals.json, the cycle-dedup fingerprint and the cached LLM
# prompts, all of which must stay byte-stable. test_dashboard_data has a
# completeness guard: every factor in universe.UNIVERSE must appear in
# BOTH maps, so universe expansions can't silently ship without blurbs.

FACTOR_LABELS: dict[str, str] = {
    "nasdaq": "Nasdaq-100",
    "sp500": "S&P 500",
    "dow": "Dow 30",
    "small-caps": "Small caps",
    "high-beta": "S&P 500 High Beta",
    "semis": "Semiconductors",
    "technology": "Technology",
    "internet": "Internet",
    "biotech": "Biotech",
    "china": "China",
    "emerging-markets": "Emerging markets",
    "financials-broad": "Financials",
    "energy": "Energy",
    "oil-gas-ep": "Oil & Gas E&P",
    "natural-gas": "Natural gas",
    "crude-oil": "Crude oil",
    "rates": "Long-dated Treasuries",
    "gold-miners": "Gold miners",
    "gold-bullion": "Gold",
    "silver": "Silver",
    "vol": "Volatility (VIX)",
    "crypto-btc": "Bitcoin",
    "crypto-eth": "Ether",
    "homebuilders": "Homebuilders",
    "defense": "Aerospace & Defense",
    "healthcare": "Healthcare",
    "regional-banks": "Regional banks",
    "utilities": "Utilities",
    "retail": "Retail",
    "brazil": "Brazil",
    "india": "India",
    "europe": "Europe",
    "korea": "South Korea",
    "nvda": "NVIDIA (single stock)",
    "tsla": "Tesla (single stock)",
    "mstr": "Strategy/MSTR (single stock)",
    "coin": "Coinbase (single stock)",
    "pltr": "Palantir (single stock)",
    "amzn": "Amazon (single stock)",
    "googl": "Alphabet (single stock)",
    "meta": "Meta (single stock)",
}

FACTOR_BLURBS: dict[str, str] = {
    "nasdaq": "The Nasdaq-100 index — the 100 largest non-financial US companies, dominated by mega-cap tech.",
    "sp500": "The S&P 500 index — the 500 largest US companies, the broadest 'US stock market' benchmark.",
    "dow": "The Dow Jones Industrial Average — 30 US blue chips, tilted to industrials and financials.",
    "small-caps": "The Russell 2000 index of US small-cap stocks — more domestic and rate-sensitive than large caps.",
    "high-beta": "The most volatile (highest-beta) stocks inside the S&P 500 — an amplified risk-on/risk-off expression.",
    "semis": "Semiconductor companies — chip designers and manufacturers (NVDA, AMD, Broadcom and the supply chain).",
    "technology": "The broad US technology sector — software, hardware and IT services.",
    "internet": "Internet companies — e-commerce, search, social media and cloud platforms.",
    "biotech": "Biotechnology companies — drug developers whose stocks hinge on trial and FDA outcomes; very volatile.",
    "china": "The FTSE China 50 — the largest Chinese companies listed in Hong Kong (Tencent, Alibaba, Meituan).",
    "emerging-markets": "The MSCI Emerging Markets index — China, India, Taiwan, Korea, Brazil and other developing markets.",
    "financials-broad": "Large US financial companies — banks, brokers, insurers and asset managers.",
    "energy": "The S&P energy sector — integrated oil & gas majors such as Exxon and Chevron.",
    "oil-gas-ep": "Oil & gas exploration-and-production companies — drillers whose earnings track crude and natgas prices.",
    "natural-gas": "Natural gas futures — the commodity itself; notoriously volatile and weather-driven.",
    "crude-oil": "WTI crude-oil futures — the oil price itself, not oil company shares.",
    "rates": "20+ year US Treasury bonds — a bet on interest rates (bond prices rise when yields fall).",
    "gold-miners": "Gold-mining companies — operationally levered to the gold price, so they swing harder than bullion.",
    "gold-bullion": "Gold bullion — the metal itself, the classic inflation and crisis hedge.",
    "silver": "Silver bullion — part precious metal, part industrial metal; more volatile than gold.",
    "vol": "VIX short-term futures — the market's 'fear gauge'; spikes in sell-offs and decays in calm markets.",
    "crypto-btc": "Bitcoin, held via BTC futures.",
    "crypto-eth": "Ether (Ethereum's token), held via ETH futures — tracks BTC loosely but regularly decouples.",
    "homebuilders": "US homebuilders and building-supply companies — sensitive to mortgage rates and housing demand.",
    "defense": "Aerospace & defense contractors — Boeing, Lockheed, RTX and peers.",
    "healthcare": "The US healthcare sector — pharma, insurers and medical-device makers; classically defensive.",
    "regional-banks": "US regional banks — smaller lenders sensitive to rates, deposits and credit conditions.",
    "utilities": "US utility companies — regulated power, gas and water providers; defensive and rate-sensitive.",
    "retail": "US retailers — big-box, e-commerce and specialty stores; a read on consumer spending.",
    "brazil": "The MSCI Brazil index — a commodity-heavy Latin American market (Petrobras, Vale, big banks).",
    "india": "The MSCI India index — the largest Indian companies (Reliance, Infosys, HDFC).",
    "europe": "Developed-Europe large caps (FTSE Europe) — a euro/ECB-sensitive regional bet.",
    "korea": "The MSCI South Korea index — an export- and semiconductor-heavy market led by Samsung.",
    "nvda": "NVIDIA — the dominant AI-chip maker; carries single-company earnings and guidance risk.",
    "tsla": "Tesla — EVs, energy storage and autonomy; a high-volatility single-company bet.",
    "mstr": "Strategy (MicroStrategy) — effectively a leveraged Bitcoin proxy via its large BTC treasury.",
    "coin": "Coinbase — the largest US crypto exchange; tracks crypto prices and trading volumes.",
    "pltr": "Palantir — government and commercial AI/data-analytics software; single-company event risk.",
    "amzn": "Amazon — e-commerce plus the AWS cloud business; single-company event risk.",
    "googl": "Alphabet (Google) — search advertising, YouTube and Google Cloud; single-company event risk.",
    "meta": "Meta — Facebook/Instagram advertising and heavy AI spend; single-company event risk.",
}


def universe_explainer_rows(
    open_pnl_by_symbol: dict[str, float | None] | None = None,
) -> list[dict]:
    """One row per universe ticker for the Portfolio tab's reference table.

    `open_pnl_by_symbol` maps currently-open symbols to their Net P&L (None
    when the position is open but unmarked). Open symbols sort to the top —
    winners first, then losers, unmarked-open last within the group — and
    carry `_status` ("win" / "loss" / "open"); everything else sorts by
    factor label A→Z with the bull leg ahead of the bear leg. `_status` is
    presentation metadata for row highlighting — the dashboard drops it
    before render.
    """
    open_pnl_by_symbol = open_pnl_by_symbol or {}
    rows: list[dict] = []
    for e in universe_lib.UNIVERSE:
        label = FACTOR_LABELS.get(e.factor, e.factor)
        blurb = FACTOR_BLURBS.get(e.factor, e.description)
        lev = f"{abs(e.leverage_factor):g}x"
        is_bull = e.leverage_factor > 0
        bull, bear = universe_lib.factor_pair(e.symbol)
        pair = bear if is_bull else bull
        if pair == e.symbol:
            pair = None
        if is_bull:
            direction_note = f"Holding it is a leveraged ({lev} daily) bullish bet."
        else:
            direction_note = (
                f"Inverse: it gains ~{lev} of what the factor loses each day "
                f"— holding it is a bearish bet."
            )
        pnl = open_pnl_by_symbol.get(e.symbol)
        if e.symbol in open_pnl_by_symbol:
            if pnl is not None and pnl > 0:
                status = "win"
            elif pnl is not None and pnl < 0:
                status = "loss"
            else:
                status = "open"
        else:
            status = ""
        rows.append({
            "Symbol": e.symbol,
            "Factor": label,
            "Direction": "Bull" if is_bull else "Bear",
            "Leverage": lev,
            "Pair": pair or "—",
            "Explainer": f"{blurb} {direction_note}",
            "Open P&L": pnl if status else None,
            "_status": status,
        })

    def _key(r: dict):
        if r["_status"]:
            pnl = r["Open P&L"]
            # Open group first; marked rows by P&L descending, unmarked last.
            return (0, 0 if pnl is not None else 1, -(pnl or 0.0), r["Factor"], r["Symbol"])
        return (1, 0, 0.0, r["Factor"], 0 if r["Direction"] == "Bull" else 1, r["Symbol"])

    # Keys are same-length within each group but differ across groups —
    # Python compares tuples lazily, and the group flag (element 0) always
    # differs before the shapes diverge, so the mixed shapes never collide.
    rows.sort(key=_key)
    return rows


# ---------- Calibration tab: self-knowledge + activity + readiness ----------


def calibration_view() -> dict:
    """Everything the Calibration tab renders about the agent's own record:
    the performance memo (factor / confidence-bucket / regime win rates,
    recent tagged exits) plus the raw kill-event audit. Mirrors exactly
    what the LLM stages are fed, so the operator sees the same evidence
    the agent sees."""
    from . import feedback

    memo = feedback.build_performance_memo_safe() or {
        "closed_trades": 0, "note": "memo unavailable",
    }
    return {
        "memo": memo,
        "kill_events": list(reversed(state.read_kill_events(limit=50))),
    }


def critic_history(limit: int = 100) -> dict:
    """Accept/reject record of the critic stage from run artifacts.

    Walks state/runs/*/critique.json newest-first (run ids sort
    chronologically). Returns {"rows": [...], "accepted": n,
    "rejected": n} — rows carry run_id, accept, and a critique snippet.
    Skipped-as-no-op critiques count as accepted (they are auto-accept
    artifacts) but keep their distinguishing text visible in the row.
    """
    rows: list[dict] = []
    runs_dir = state.RUNS_DIR
    if runs_dir.exists():
        for run_dir in sorted(runs_dir.iterdir(), reverse=True):
            if len(rows) >= limit:
                break
            p = run_dir / "critique.json"
            if not p.exists():
                continue
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            rows.append({
                "run_id": run_dir.name,
                "accept": bool(c.get("accept", True)),
                "critique": (c.get("critique") or "")[:300],
                "suggested_changes": len(c.get("suggested_changes") or []),
            })
    return {
        "rows": rows,
        "accepted": sum(1 for r in rows if r["accept"]),
        "rejected": sum(1 for r in rows if not r["accept"]),
    }


def activity_metrics(nav_rows: list[dict] | None = None, *, max_runs: int = 200) -> dict:
    """Is the system actually trading? The original build over-gated into
    chronic all-cash; these numbers make any regression toward that
    visible at a glance.

      - pct_cycles_with_orders: of recent runs that wrote an order plan,
        how many submitted at least one order leg
      - dedup_skipped: runs short-circuited by the cycle dedup
      - time_in_market_pct: share of NAV-history rows holding >=1 position
      - avg_positions / avg_cash_pct: deployment depth over the same rows
    """
    if nav_rows is None:
        nav_rows = state.read_nav_history()

    runs_seen = orders_cycles = dedup_skips = 0
    runs_dir = state.RUNS_DIR
    if runs_dir.exists():
        for run_dir in sorted(runs_dir.iterdir(), reverse=True)[:max_runs]:
            p = run_dir / "next_run.json"
            if not p.exists():
                continue
            try:
                nr = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            runs_seen += 1
            if nr.get("dedup_skipped"):
                dedup_skips += 1
                continue
            plan = nr.get("order_plan") or {}
            submitted = [
                r for r in (plan.get("results") or [])
                if not str(r.get("status", "")).startswith(("error", "skipped"))
            ]
            if submitted:
                orders_cycles += 1

    in_market = [r for r in nav_rows if (r.get("positions_count") or 0) > 0]
    cash_pcts = [
        (r.get("cash_usd") or 0.0) / r["nav_usd"] * 100.0
        for r in nav_rows
        if isinstance(r.get("nav_usd"), (int, float)) and r["nav_usd"] > 0
    ]
    n_nav = len(nav_rows)
    return {
        "runs_seen": runs_seen,
        "cycles_with_orders": orders_cycles,
        "pct_cycles_with_orders": (
            round(100.0 * orders_cycles / runs_seen, 1) if runs_seen else None
        ),
        "dedup_skipped": dedup_skips,
        "time_in_market_pct": (
            round(100.0 * len(in_market) / n_nav, 1) if n_nav else None
        ),
        "avg_positions": (
            round(sum((r.get("positions_count") or 0) for r in nav_rows) / n_nav, 2)
            if n_nav else None
        ),
        "avg_cash_pct": (
            round(sum(cash_pcts) / len(cash_pcts), 1) if cash_pcts else None
        ),
    }


def trade_sync_gaps(*, lookback_days: int = 7) -> dict:
    """Detect a silent trade-sync blackout: runs that SUBMITTED accepted
    orders within the lookback window but have zero matching fills in
    trades.jsonl. A 5-week gap like this happened once (May 2026) and
    silently broke cooldown, P&L and the Sharpe gate — this surfaces it
    on the dashboard the day it starts.

    Returns {"stale": bool, "gaps": [{run_id, order_ids}], ...}.
    """
    from datetime import timedelta

    cutoff = (state.utcnow() - timedelta(days=lookback_days)).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    runs_with_fills = {
        r.get("run_id") for r in state.read_trades() if r.get("run_id")
    }
    gaps: list[dict] = []
    runs_dir = state.RUNS_DIR
    if runs_dir.exists():
        for run_dir in sorted(runs_dir.iterdir(), reverse=True):
            # Run ids start with the UTC timestamp — lexicographic compare
            # against the cutoff prunes old runs cheaply.
            if run_dir.name < cutoff:
                break
            p = run_dir / "orders.json"
            if not p.exists():
                continue
            try:
                orders = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            order_ids = orders.get("order_ids") or []
            if order_ids and run_dir.name not in runs_with_fills:
                gaps.append({"run_id": run_dir.name, "order_ids": order_ids})
    return {
        "stale": bool(gaps),
        "gaps": gaps,
        "lookback_days": lookback_days,
    }


def readiness_scorecard(nav_rows: list[dict] | None = None) -> list[dict]:
    """Auto-tracked promotion-to-live criteria (CLAUDE.md §Promotion).

    Each row: {criterion, target, value, met} with met in {True, False,
    None} (None = not enough data to evaluate). The cycle-completion
    criterion is approximated as "weekdays with >=1 completed cycle /
    weekdays elapsed" since the scheduler's intended cadence isn't
    persisted. Purely informational — promotion remains a manual,
    triple-locked decision.
    """
    from datetime import timedelta

    from . import benchmark

    if nav_rows is None:
        nav_rows = state.read_nav_history()

    rows: list[dict] = []

    # --- continuous running >= 4 weeks ---
    span_days = None
    if len(nav_rows) >= 2:
        try:
            first = benchmark._parse_iso_utc(nav_rows[0]["at"])
            last = benchmark._parse_iso_utc(nav_rows[-1]["at"])
            span_days = (last - first).days
        except (ValueError, TypeError, KeyError):
            span_days = None
    rows.append({
        "criterion": "Continuous paper running",
        "target": ">= 28 days",
        "value": f"{span_days} days" if span_days is not None else "—",
        "met": (span_days >= 28) if span_days is not None else None,
    })

    # --- cycle completion >= 80% (weekday-coverage proxy) ---
    completion = None
    if len(nav_rows) >= 2 and span_days is not None:
        try:
            first_d = benchmark._parse_iso_utc(nav_rows[0]["at"]).date()
            last_d = benchmark._parse_iso_utc(nav_rows[-1]["at"]).date()
            weekdays = [
                first_d + timedelta(days=i)
                for i in range((last_d - first_d).days + 1)
            ]
            weekdays = [d for d in weekdays if d.weekday() < 5]
            covered = {
                str(r.get("at") or "")[:10] for r in nav_rows
            }
            hit = sum(1 for d in weekdays if d.isoformat() in covered)
            completion = 100.0 * hit / len(weekdays) if weekdays else None
        except (ValueError, TypeError):
            completion = None
    rows.append({
        "criterion": "Cycle completion (weekday coverage proxy)",
        "target": ">= 80%",
        "value": f"{completion:.0f}%" if completion is not None else "—",
        "met": (completion >= 80.0) if completion is not None else None,
    })

    # --- Sharpe >= 0.5 and max DD <= 25% from the EOD equity curve ---
    sharpe_v = dd_pct = None
    try:
        eod = benchmark.align_to_eod(nav_rows)
        if len(eod) >= 10:
            returns = eod["nav"].pct_change().dropna()
            sharpe_v = benchmark.sharpe(returns)
            dd, _, _ = benchmark.max_drawdown(eod["nav"])
            dd_pct = abs(dd) * 100.0
    except Exception:
        pass
    rows.append({
        "criterion": "Sharpe (rf=0, EOD synthetic NAV)",
        "target": ">= 0.5",
        "value": f"{sharpe_v:.2f}" if sharpe_v is not None else "— (need >= 10 EOD points)",
        "met": (sharpe_v >= 0.5) if sharpe_v is not None else None,
    })
    rows.append({
        "criterion": "Max drawdown",
        "target": "<= 25%",
        "value": f"{dd_pct:.1f}%" if dd_pct is not None else "—",
        "met": (dd_pct <= 25.0) if dd_pct is not None else None,
    })

    # --- no failures in the last 7 days ---
    cutoff_iso = (state.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    failures = 0
    for d in load_decisions(limit=2000):
        if (d.get("started_at") or "") < cutoff_iso:
            continue
        status = str(d.get("status") or "")
        if status.startswith("error") or "fail" in status:
            failures += 1
    rows.append({
        "criterion": "Unresolved failures (last 7 days)",
        "target": "0",
        "value": str(failures),
        "met": failures == 0,
    })
    return rows
