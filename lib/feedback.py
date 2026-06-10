"""Performance memo — the agent's own track record, fed back as LLM context.

The system has always computed win rate / profit factor / per-trade PnL for
the dashboard, but no LLM stage ever saw any of it: the strategist's only
feedback was the last 5 cycles' regime + NAV %. This module turns
state/trades.jsonl + state/costs.jsonl + run artifacts into a compact,
deterministic memo the strategist and constructor read every cycle:

  - overall realized record (win rate, profit factor, avg win/loss, hold)
  - per-factor record (which factors the agent actually makes money on)
  - confidence calibration (how often its 0.7+ picks actually won) — the
    entry confidence is joined from the opening run's view.json, so no
    schema change to trades.jsonl is needed
  - recent exits, tagged with the exit machinery that fired them when a
    monitor kill event matches (loss cap / price stop / time stop /
    trailing stop), else "agent_decision" (orchestrator rebalance/harvest)

The memo is EVIDENCE for the agent's judgment on conviction and sizing —
the prompts explicitly frame it as calibration input, not a mandate to
trade less. Cost: $0 — pure Python over existing state.

Everything here is defensive: a missing/corrupt artifact degrades a field
to None/empty, never raises into the pipeline.
"""
from __future__ import annotations

import json
from collections import defaultdict

from . import state, trades, universe

# Bucket edges for confidence calibration. Labels are half-open ranges;
# trades whose opening run artifact can't be found land in "unknown".
_CONFIDENCE_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.5, "<0.50"),
    (0.5, 0.7, "0.50-0.69"),
    (0.7, 0.85, "0.70-0.84"),
    (0.85, 1.01, "0.85+"),
)

# How close (in seconds) a monitor kill event must be to a trade's
# closed_at to be treated as the cause of that exit. Monitor flattens
# fill within minutes; 6h absorbs sync lag without cross-matching
# unrelated exits days apart.
_KILL_EVENT_MATCH_WINDOW_S = 6 * 3600.0


def _view_info_for(run_id: str | None, *, _cache: dict | None = None) -> dict:
    """{candidates: {symbol: confidence}, regime: str|None} for a run's
    view.json. Empty/None values when the run/artifact is missing or
    corrupt. `_cache` (optional dict keyed by run_id) avoids re-reading
    the same view.json once per fill."""
    empty = {"candidates": {}, "regime": None}
    if not run_id:
        return empty
    cache = _cache if _cache is not None else {}
    if run_id not in cache:
        view_path = state.RUNS_DIR / run_id / "view.json"
        info = {"candidates": {}, "regime": None}
        if view_path.exists():
            try:
                view = json.loads(view_path.read_text(encoding="utf-8"))
                for c in view.get("candidates") or []:
                    sym = c.get("symbol")
                    conf = c.get("confidence")
                    if isinstance(sym, str) and isinstance(conf, (int, float)):
                        info["candidates"][sym] = float(conf)
                regime = view.get("regime")
                if isinstance(regime, str):
                    info["regime"] = regime
            except (json.JSONDecodeError, OSError):
                pass
        cache[run_id] = info
    return cache[run_id]


def entry_confidence_for(run_id: str | None, symbol: str,
                         *, _cache: dict | None = None) -> float | None:
    """Strategist confidence for `symbol` in the run that opened it.
    None when the run/artifact/candidate is missing — legacy trades
    predate this linkage and land in the "unknown" bucket."""
    return _view_info_for(run_id, _cache=_cache)["candidates"].get(symbol)


def _confidence_bucket(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    for lo, hi, label in _CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return label
    return "unknown"


def _round(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def _exit_kind_for(ct: trades.ClosedTrade, kill_events: list[dict]) -> str:
    """Best-effort attribution of a closed trade to the exit machinery
    that fired it. A monitor kill event for the same symbol within the
    match window of closed_at wins; otherwise the close was an ordinary
    orchestrator decision (rebalance / harvest / regime change)."""
    closed_dt = trades._parse_iso_utc(ct.closed_at)
    if closed_dt is None:
        return "agent_decision"
    for ev in kill_events:
        if ev.get("symbol") != ct.symbol:
            continue
        ev_dt = trades._parse_iso_utc(ev.get("at"))
        if ev_dt is None:
            continue
        if abs((closed_dt - ev_dt).total_seconds()) <= _KILL_EVENT_MATCH_WINDOW_S:
            return ev.get("exit_kind") or "kill_event"
    return "agent_decision"


def _record(rows: list[trades.ClosedTrade]) -> dict:
    """Win/loss record over a set of closed trades (net-PnL basis,
    consistent with the dashboard's trade_stats)."""
    nets = [t.net_pnl_usd for t in rows]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": _round(100.0 * len(wins) / len(rows)) if rows else None,
        "net_pnl_usd": _round(sum(nets)),
        "avg_win_usd": _round(sum(wins) / len(wins)) if wins else None,
        "avg_loss_usd": _round(sum(losses) / len(losses)) if losses else None,
        "profit_factor": _round(sum(wins) / abs(sum(losses))) if losses and wins else None,
    }


def build_performance_memo(
    *,
    trade_rows: list[dict] | None = None,
    cost_rows: list[dict] | None = None,
    kill_events: list[dict] | None = None,
    recent_exits_limit: int = 6,
) -> dict:
    """Build the per-cycle performance memo from the trade log.

    All inputs default to reading live state; tests inject rows directly.
    Returns a compact dict (rounded floats, capped lists) suitable for
    direct JSON inlining into the strategist/constructor user message.
    """
    if trade_rows is None:
        trade_rows = state.read_trades()
    if cost_rows is None:
        # RAW cost log — deliberately NOT state.filter_costs_post_reset.
        # That filter implements the dashboard's display-only "reset all
        # LLM costs" button; this memo is pipeline-facing calibration
        # evidence, and hiding historical costs in the UI must not make
        # past trades look more profitable to the agents (same principle
        # as lib.dashboard_data._raw_llm_cost_total_usd for sizing NAV;
        # Codex P2, PR #109). Corrupt lines are skipped, not fatal.
        cost_rows = []
        if state.COSTS_LOG.exists():
            for line in state.COSTS_LOG.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    cost_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if kill_events is None:
        kill_events = state.read_kill_events(limit=200)

    rows = sorted(trade_rows, key=lambda r: r.get("filled_at") or "")
    pnl = trades.compute_trades_pnl(rows, costs=cost_rows)
    closed = pnl.closed

    if not closed:
        return {
            "closed_trades": 0,
            "note": "no closed trades yet — no track record to calibrate against",
        }

    by_factor: dict[str, list[trades.ClosedTrade]] = defaultdict(list)
    for ct in closed:
        entry = universe.by_symbol(ct.symbol)
        by_factor[entry.factor if entry else "unknown"].append(ct)

    view_cache: dict = {}
    by_bucket: dict[str, list[trades.ClosedTrade]] = defaultdict(list)
    by_regime: dict[str, list[trades.ClosedTrade]] = defaultdict(list)
    for ct in closed:
        info = _view_info_for(ct.buy_run_id, _cache=view_cache)
        by_bucket[_confidence_bucket(info["candidates"].get(ct.symbol))].append(ct)
        by_regime[info["regime"] or "unknown"].append(ct)

    # Hold time on the same basis as the dashboard's trade_stats.
    hold_hours: list[float] = []
    for ct in closed:
        o = trades._parse_iso_utc(ct.opened_at)
        c = trades._parse_iso_utc(ct.closed_at)
        if o is not None and c is not None and c >= o:
            hold_hours.append((c - o).total_seconds() / 3600.0)

    recent = sorted(closed, key=lambda t: t.closed_at)[-recent_exits_limit:]
    recent_exits = [
        {
            "symbol": ct.symbol,
            "closed_at": ct.closed_at,
            "net_pnl_usd": _round(ct.net_pnl_usd),
            "exit_kind": _exit_kind_for(ct, kill_events),
        }
        for ct in reversed(recent)  # newest first — the tape the LLM reads
    ]

    overall = _record(closed)
    overall["avg_hold_hours"] = (
        _round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else None
    )

    bucket_order = [label for _, _, label in _CONFIDENCE_BUCKETS] + ["unknown"]
    return {
        "closed_trades": len(closed),
        "overall": overall,
        "by_factor": [
            {"factor": f, **_record(rows_f)}
            for f, rows_f in sorted(
                by_factor.items(), key=lambda kv: len(kv[1]), reverse=True,
            )
        ],
        "confidence_calibration": [
            {"bucket": b, **_record(by_bucket[b])}
            for b in bucket_order if b in by_bucket
        ],
        "by_regime": [
            {"regime": r, **_record(rows_r)}
            for r, rows_r in sorted(
                by_regime.items(), key=lambda kv: len(kv[1]), reverse=True,
            )
        ],
        "recent_exits": recent_exits,
    }


def build_performance_memo_safe() -> dict | None:
    """Pipeline-facing wrapper: never raises, returns None on any failure
    so a corrupt state file can't take down a trading cycle."""
    try:
        return build_performance_memo()
    except Exception:
        return None
