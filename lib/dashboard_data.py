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
        },
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
    # Live broker NAV — best-effort; if get_account fails we still have
    # the positions data, just fall back to portfolio.json snapshot at
    # render time.
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
    return f"{pos['underlying']}|{pos['strike']}|{pos['expiry']}|{pos['type']}"


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
          cost_usd
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

    summaries: list[dict] = []
    for run_dir in run_dirs:
        rid = run_dir.name
        s: dict = {
            "run_id": rid,
            "generated_at": "",
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
                    s["generated_at"] = p.get("generated_at", "") or ""
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
        view_path = run_dir / "view.json"
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


def position_table_rows(
    portfolio: dict,
    marks: dict[str, float] | None = None,
    costs: dict[str, float] | None = None,
    held_keys: frozenset[str] | set[str] | None = None,
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
    out: list[dict] = []
    for p in portfolio.get("positions", []):
        # Stale-position filter: when the broker is reachable and reports
        # which keys it still holds, hide portfolio.json rows the broker
        # no longer carries (manual close, kill-condition exit, expiry).
        # held_keys=None means "broker unreachable, don't filter" — we
        # render everything in that case rather than blank the dashboard.
        if held_keys is not None and mark_key_for_position(p) not in held_keys:
            continue
        if p["kind"] == "etf":
            key = p["symbol"]
            mark = marks.get(key)
            broker_cost = costs.get(key)
            cost_per_unit = broker_cost if broker_cost is not None else p["avg_cost"]
            shares = p["shares"]
            row = {
                "Symbol": p["symbol"],
                "Kind": "ETF",
                "Leverage": f"{p.get('leverage_factor', 1):g}x",
                "Qty": shares,
                "Cost": cost_per_unit,
                "Notional": shares * cost_per_unit,
                "% NAV": p["position_pct"],
                "Greeks": "—",
                "Kill": f"≤{p['kill_conditions']['max_loss_pct']}%",
            }
        else:
            contracts = p["contracts"]
            g = p["greeks"]
            # Look up via synthetic key (the convention the rest of the
            # codebase uses); fall back to OSI for backwards compat.
            synth_key = f"{p['underlying']}|{p['strike']}|{p['expiry']}|{p['type']}"
            mark = marks.get(synth_key)
            broker_cost = costs.get(synth_key)
            if mark is None or broker_cost is None:
                try:
                    osi = osi_symbol(
                        underlying=p["underlying"], expiry=p["expiry"],
                        type=p["type"], strike=p["strike"],
                    )
                    if mark is None:
                        mark = marks.get(osi)
                    if broker_cost is None:
                        broker_cost = costs.get(osi)
                except (ValueError, KeyError):
                    pass
            cost_per_unit = broker_cost if broker_cost is not None else p["premium_paid"]
            premium_usd = cost_per_unit * 100 * contracts
            row = {
                "Symbol": f"{p['underlying']} {p['type'].upper()} {p['strike']} {p['expiry']}",
                "Kind": "OPT",
                "Leverage": "—",
                "Qty": contracts,
                "Cost": cost_per_unit,
                "Notional": premium_usd,
                "% NAV": p["position_pct"],
                "Greeks": (
                    f"Δ{g['delta']:.2f} Θ{g['theta']:.2f} "
                    f"IV {g['iv']*100:.0f}% (p{int(g['iv_percentile'])})"
                ),
                "Kill": f"≤{p['kill_conditions']['max_loss_pct']}%",
            }
        # Pass the broker-truth cost basis into the P&L helper so option
        # P&L reflects actual fill, not the agent's premium estimate.
        breakdown = pnl_lib.compute_position_pnl(
            position=p,
            current_mark_usd=mark,
            actual_cost_per_unit=cost_per_unit,
        )
        row["Mark"] = mark if mark is not None else None
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
