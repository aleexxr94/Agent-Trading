#!/usr/bin/env python3
"""Read-only performance assessment against the CLAUDE.md §Promotion gates.

Composes the existing analysis helpers (``lib.dashboard_data``,
``lib.feedback``, ``lib.benchmark``, ``lib.trades``) into a single
reproducible report so a human — or Claude Code running on the VPS — can
judge how the paper account is doing and where it diverges from the build
spec. See ``deploy/assess.md`` for the SSH + Claude-Code runbook that wraps
this.

This tool is STRICTLY READ-ONLY: it reads ``state/`` logs and computes /
formats. It never writes ``state/``, never calls the broker order path,
never touches services, and never prints secrets. The one optional write is
the ``--json`` machine-readable dump, and only to the path you name.

Every statistic carries its sample size (N). On a $2,500 paper account a
few weeks in, most numbers are variance, not signal — the report prints an
explicit low-sample banner so findings are read as directional, not
actionable. The math is deliberately delegated to the same functions the
dashboard uses (``readiness_scorecard`` etc.) so week-over-week runs are
comparable rather than re-derived each time.

Run:
    python -m bin.assess_performance [--limit N] [--json path]

Prints a Markdown report to stdout. ``--json`` additionally writes the
machine-readable assessment dict (suitable for the Claude layer or a future
weekly timer to consume without re-parsing prose).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import state  # noqa: E402

# Below this many closed trades (or fewer than this many days running) the
# report treats every conclusion as directional. 20 closed trades / 28 days
# mirrors the scorecard's own "needs >= N" gates and the §11 4-week floor.
LOW_N_TRADES = 20
LOW_N_DAYS = 28


def _safe(fn: Callable[[], Any]) -> Any:
    """Run a section builder; degrade to an ``{error: ...}`` marker rather
    than aborting the whole report on one bad log / offline network."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — a report must never crash
        return {"error": f"{type(exc).__name__}: {exc}"}


def _span_days(nav_rows: list[dict]) -> int | None:
    from lib import benchmark
    if len(nav_rows) < 2:
        return None
    try:
        first = benchmark._parse_iso_utc(nav_rows[0]["at"])
        last = benchmark._parse_iso_utc(nav_rows[-1]["at"])
        return (last - first).days
    except (ValueError, TypeError, KeyError):
        return None


def _era() -> dict:
    """Paper vs live era from the write-once transition marker."""
    lt = state.read_live_transition()
    if not lt:
        return {"mode": "paper", "live_since": None}
    return {"mode": "live", "live_since": lt.get("at") or lt.get("transitioned_at")}


def _returns_risk(bundle) -> dict:
    """Scalar slice of the SPY-comparison MetricsBundle (JSON-safe).

    Takes the bundle prebuilt by ``gather()`` (one yfinance fetch per
    report, shared with the promotion scorecard) — degrades to
    ``available: false`` when it couldn't be built (too little history,
    or offline)."""
    if bundle is None:
        return {"available": False, "reason": "bundle unavailable (too little history or offline)"}
    dd_pct, dd_peak, dd_trough = bundle.max_dd_strategy
    return {
        "available": True,
        "inception": str(bundle.inception),
        "as_of": str(bundle.as_of),
        "starting_balance_usd": bundle.starting_balance_usd,
        "strategy_total_return_pct": bundle.strategy_total_return_pct,
        "spy_total_return_pct": bundle.spy_total_return_pct,
        "delta_vs_spy_pct": bundle.delta_pct,
        "cagr_strategy": bundle.cagr_strategy,
        "sharpe_strategy": bundle.sharpe_strategy,
        "sharpe_spy": bundle.sharpe_spy,
        "vol_strategy_ann": bundle.vol_strategy_ann,
        "max_drawdown_pct": abs(dd_pct) * 100.0,
        "max_drawdown_peak": str(dd_peak),
        "max_drawdown_trough": str(dd_trough),
        "correlation_spy": bundle.correlation,
        "pct_months_beat_spy": bundle.pct_months_strategy_beat,
    }


def _trade_record() -> dict:
    from lib import dashboard_data

    view = dashboard_data.trades_pnl_view()
    stats = dashboard_data.trade_stats(view.get("closed") or [])
    return {"totals": view.get("totals"), "stats": stats}


def _cost_health() -> dict:
    from lib import dashboard_data

    trend = dashboard_data.cache_hit_trend(limit=200)
    cache_latest = trend[-1]["cache_hit_pct"] if trend else None
    cache_avg = (
        sum(r["cache_hit_pct"] for r in trend) / len(trend) if trend else None
    )
    return {
        "token_totals": dashboard_data.total_token_cost(),
        "by_stage": dashboard_data.cost_by_stage(),
        "cache_hit_latest_pct": cache_latest,
        "cache_hit_avg_pct": cache_avg,
        "trading_fees_usd": dashboard_data.total_trading_fees_usd(),
        "slippage_usd": dashboard_data.total_slippage_usd(),
    }


def _incomplete_cycles(*, lookback_days: int = 7, min_age_hours: int = 3) -> dict:
    """Crashed / never-finished cycles — delegates to the shared
    ``dashboard_data.incomplete_cycles`` so this CLI, the dashboard, and
    the ``readiness_scorecard`` failure gate all count identically
    (closed-market cycles excluded, crash-handler ``error.json`` dirs
    excluded to avoid double counting their decision rows)."""
    from lib import dashboard_data

    return dashboard_data.incomplete_cycles(
        lookback_days=lookback_days, min_age_hours=min_age_hours,
    )


def _operational() -> dict:
    """Cycle deployment + failure/status counts over the last 7 days.

    The failure count and status histogram come from the shared
    ``dashboard_data.failure_summary`` — the same evidence the
    ``readiness_scorecard`` failure gate uses, so this report can never
    show a green gate while counting failures differently. Since the
    pipeline crash handler exists, unhandled crashes appear here as
    status="error" rows; cost-cap stops appear as "aborted" (a designed
    guardrail, deliberately not a gate failure)."""
    from lib import dashboard_data

    fs = dashboard_data.failure_summary(days=7)
    return {
        "activity": dashboard_data.activity_metrics(),
        "failures_last_7d": fs["failures"],
        "incomplete_cycles_last_7d": fs["incomplete"],
        "status_counts_last_7d": fs["status_counts"],
        "trade_sync_gaps": dashboard_data.trade_sync_gaps(),
    }


def gather(limit: int | None = None) -> dict:
    """Build the full, JSON-serialisable assessment dict. Read-only."""
    from bin import analyze_runs
    from lib import dashboard_data, feedback

    nav_rows = state.read_nav_history()
    span = _span_days(nav_rows)
    # One SPY-dense bundle per report, shared by the scorecard's primary
    # Sharpe/DD gate rows and the returns_risk section (single fetch;
    # None → both degrade to their labeled offline fallbacks).
    try:
        bench_bundle = dashboard_data.benchmark_view()
    except Exception:
        bench_bundle = None
    memo = _safe(feedback.build_performance_memo)
    closed_trades = (
        memo.get("closed_trades", 0) if isinstance(memo, dict) else 0
    )
    low_n = (closed_trades < LOW_N_TRADES) or (span is None) or (span < LOW_N_DAYS)

    return {
        "meta": {
            "generated_at": state.utcnow_iso(),
            "era": _era(),
            "days_running": span,
            "nav_points": len(nav_rows),
            "closed_trades": closed_trades,
            "low_sample": low_n,
            "low_sample_thresholds": {
                "min_closed_trades": LOW_N_TRADES,
                "min_days": LOW_N_DAYS,
            },
        },
        "promotion_scorecard": _safe(
            lambda: dashboard_data.readiness_scorecard(nav_rows, bundle=bench_bundle)
        ),
        "returns_risk": _safe(lambda: _returns_risk(bench_bundle)),
        "trade_record": _safe(_trade_record),
        "calibration": memo,
        "cost_health": _safe(_cost_health),
        "operational": _safe(_operational),
        "cycles": _safe(lambda: analyze_runs.collect(limit=limit)),
    }


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------

def _fmt(v: Any, spec: str = "") -> str:
    if v is None:
        return "—"
    if spec and isinstance(v, (int, float)):
        return format(v, spec)
    return str(v)


def _render(report: dict) -> str:
    out: list[str] = []
    p = out.append

    meta = report.get("meta", {})
    era = meta.get("era", {})
    p("# Performance Assessment")
    p("")
    p("> **PAPER TRADING — read-only assessment. Leveraged & inverse ETFs on a "
      "small account are high-risk. Not financial advice.**")
    p("")
    p(f"- Generated: `{meta.get('generated_at', '—')}`")
    mode = era.get("mode", "paper")
    since = f" (since {era.get('live_since')})" if era.get("live_since") else ""
    p(f"- Era: **{mode}**{since}")
    p(f"- Days running: {_fmt(meta.get('days_running'))} · "
      f"NAV points: {meta.get('nav_points', 0)} · "
      f"Closed trades: {meta.get('closed_trades', 0)}")
    if meta.get("low_sample"):
        th = meta.get("low_sample_thresholds", {})
        p("")
        p(f"> ⚠️ **LOW SAMPLE** — fewer than {th.get('min_closed_trades')} closed "
          f"trades or {th.get('min_days')} days of history. Treat every number "
          f"below as **directional, not actionable**. Most variation at this "
          f"size is noise, not signal — do not tune prompts to it.")

    # --- Promotion scorecard -------------------------------------------
    p("")
    p("## §11 Promotion scorecard")
    sc = report.get("promotion_scorecard")
    if isinstance(sc, dict) and "error" in sc:
        p(f"_unavailable: {sc['error']}_")
    elif sc:
        p("")
        p("| Criterion | Target | Value | Met |")
        p("|---|---|---|---|")
        for row in sc:
            met = row.get("met")
            badge = "✅" if met is True else "❌" if met is False else "⏳ n/a"
            p(f"| {row.get('criterion', '—')} | {row.get('target', '—')} "
              f"| {row.get('value', '—')} | {badge} |")
        p("")
        p("_Manual gates (Alpaca eligibility, triple-lock) are operator actions "
          "and are not auto-checked here._")
    else:
        p("_no data_")

    # --- Returns & risk -------------------------------------------------
    p("")
    p("## Returns & risk (vs SPY)")
    rr = report.get("returns_risk", {})
    if isinstance(rr, dict) and rr.get("error"):
        p(f"_unavailable: {rr['error']}_")
    elif not rr.get("available"):
        p(f"_unavailable: {rr.get('reason', 'insufficient history / offline')}_")
    else:
        p(f"- Window: {rr['inception']} → {rr['as_of']} "
          f"(from ${_fmt(rr['starting_balance_usd'], ',.0f')})")
        p(f"- Strategy total return: **{_fmt(rr['strategy_total_return_pct'], '+.2f')}%** "
          f"vs SPY {_fmt(rr['spy_total_return_pct'], '+.2f')}% "
          f"(Δ {_fmt(rr['delta_vs_spy_pct'], '+.2f')}%)")
        p(f"- Sharpe (rf=0): **{_fmt(rr['sharpe_strategy'], '.2f')}** "
          f"vs SPY {_fmt(rr['sharpe_spy'], '.2f')}")
        p(f"- Max drawdown: **{_fmt(rr['max_drawdown_pct'], '.1f')}%** "
          f"({rr['max_drawdown_peak']} → {rr['max_drawdown_trough']})")
        p(f"- Annualised vol: {_fmt(rr['vol_strategy_ann'], '.2%')} · "
          f"Correlation to SPY: {_fmt(rr['correlation_spy'], '.2f')} · "
          f"% months beating SPY: {_fmt(rr['pct_months_beat_spy'], '.0f')}")

    # --- Trade record ---------------------------------------------------
    p("")
    p("## Trade record (net of modelled costs)")
    tr = report.get("trade_record", {})
    if isinstance(tr, dict) and tr.get("error"):
        p(f"_unavailable: {tr['error']}_")
    else:
        totals = tr.get("totals") or {}
        stats = tr.get("stats")
        p(f"- Closed: {totals.get('closed_count', 0)} · "
          f"Open: {totals.get('open_count', 0)} · "
          f"Unmatched sells: {totals.get('unmatched_sell_count', 0)}")
        if stats:
            p(f"- Win rate: **{_fmt(stats['win_rate_pct'], '.0f')}%** "
              f"(N={stats['wins'] + stats['losses']}, "
              f"{stats['wins']}W / {stats['losses']}L) · "
              f"Profit factor: {_fmt(stats['profit_factor'], '.2f')}")
            p(f"- Avg win: {_fmt(stats['avg_win_usd'], '+.2f')} · "
              f"Avg loss: {_fmt(stats['avg_loss_usd'], '+.2f')} · "
              f"Avg hold: {_fmt(stats['avg_hold_hours'], '.1f')}h")
            p(f"- Best: {stats['best']['symbol']} "
              f"({_fmt(stats['best']['net_pnl_usd'], '+.2f')}) · "
              f"Worst: {stats['worst']['symbol']} "
              f"({_fmt(stats['worst']['net_pnl_usd'], '+.2f')})")
        else:
            p("- _no closed trades yet_")
        p(f"- Realised net: {_fmt(totals.get('realised_net_usd'), '+.2f')} "
          f"(gross {_fmt(totals.get('realised_gross_usd'), '+.2f')}, "
          f"fees {_fmt(totals.get('realised_fees_usd'), '.2f')}, "
          f"slippage {_fmt(totals.get('realised_slippage_usd'), '.2f')}, "
          f"LLM {_fmt(totals.get('realised_llm_cost_usd'), '.2f')})")

    # --- Calibration & factors -----------------------------------------
    p("")
    p("## Calibration & factors")
    memo = report.get("calibration", {})
    if isinstance(memo, dict) and memo.get("error"):
        p(f"_unavailable: {memo['error']}_")
    elif memo.get("closed_trades", 0) == 0:
        p("_no closed trades yet — no track record to calibrate against_")
    else:
        conf = memo.get("confidence_calibration") or []
        if conf:
            p("")
            p("**Confidence calibration** (is stated confidence predictive?)")
            p("")
            p("| Bucket | Trades | Win rate | Net P&L |")
            p("|---|---|---|---|")
            for r in conf:
                p(f"| {r['bucket']} | {r['trades']} | "
                  f"{_fmt(r.get('win_rate_pct'), '.0f')}% | "
                  f"{_fmt(r.get('net_pnl_usd'), '+.2f')} |")
        fac = memo.get("by_factor") or []
        if fac:
            p("")
            p("**By factor** (top by trade count)")
            p("")
            p("| Factor | Trades | Win rate | Net P&L | Profit factor |")
            p("|---|---|---|---|---|")
            for r in fac[:12]:
                p(f"| {r['factor']} | {r['trades']} | "
                  f"{_fmt(r.get('win_rate_pct'), '.0f')}% | "
                  f"{_fmt(r.get('net_pnl_usd'), '+.2f')} | "
                  f"{_fmt(r.get('profit_factor'), '.2f')} |")
        exits = memo.get("recent_exits") or []
        if exits:
            p("")
            p("**Recent exits** (newest first, tagged by what killed them)")
            p("")
            for r in exits:
                tag = r.get("exit_kind", "?")
                mode_s = f" [{r['mode']}]" if r.get("mode") else ""
                p(f"- {r['symbol']}{mode_s}: {_fmt(r.get('net_pnl_usd'), '+.2f')} "
                  f"via `{tag}` @ {r.get('closed_at', '—')}")
        if memo.get("era_split"):
            p("")
            p(f"_Era split present (paper vs live since "
              f"{memo['era_split'].get('live_since')})._")

    # --- Cost health ----------------------------------------------------
    p("")
    p("## Cost health")
    ch = report.get("cost_health", {})
    if isinstance(ch, dict) and ch.get("error"):
        p(f"_unavailable: {ch['error']}_")
    else:
        tt = ch.get("token_totals") or {}
        p(f"- All-time LLM cost: **${_fmt(tt.get('cost_usd'), '.2f')}** "
          f"over {tt.get('calls', 0)} calls "
          f"(caps: $3.00/run, $12.00/day)")
        p(f"- Prompt-cache hit: latest {_fmt(ch.get('cache_hit_latest_pct'), '.0f')}% · "
          f"avg {_fmt(ch.get('cache_hit_avg_pct'), '.0f')}%")
        p(f"- Trading fees: ${_fmt(ch.get('trading_fees_usd'), '.2f')} · "
          f"Modelled slippage: ${_fmt(ch.get('slippage_usd'), '.2f')}")
        by_stage = ch.get("by_stage") or []
        if by_stage:
            p("")
            p("| Stage | Calls | Cost $ | Cache hit % |")
            p("|---|---|---|---|")
            for r in by_stage:
                p(f"| {r['stage']} | {r['calls']} | "
                  f"{_fmt(r['cost_usd'], '.2f')} | "
                  f"{_fmt(r['cache_hit_pct'], '.0f')} |")

    # --- Operational health --------------------------------------------
    p("")
    p("## Operational health")
    op = report.get("operational", {})
    if isinstance(op, dict) and op.get("error"):
        p(f"_unavailable: {op['error']}_")
    else:
        act = op.get("activity") or {}
        p(f"- Time in market: {_fmt(act.get('time_in_market_pct'), '.0f')}% · "
          f"Cycles with orders: {_fmt(act.get('pct_cycles_with_orders'), '.0f')}% · "
          f"Avg positions: {_fmt(act.get('avg_positions'))} · "
          f"Avg cash: {_fmt(act.get('avg_cash_pct'), '.0f')}%")
        inc = op.get("incomplete_cycles_last_7d") or {}
        inc_n = inc.get("count", 0)
        caveat = (
            f" — ⚠️ but {inc_n} cycle(s) crashed without logging a decision "
            f"row, so this under-reports; see incomplete cycles below"
            if inc_n else ""
        )
        p(f"- Failures (last 7d): **{op.get('failures_last_7d', 0)}** "
          f"(target 0){caveat}")
        p(f"- Incomplete/crashed cycles (last 7d): **{inc_n}** "
          f"(started a stage but never wrote next_run.json)")
        sc_counts = op.get("status_counts_last_7d") or {}
        if sc_counts:
            pretty = ", ".join(f"{k}={v}" for k, v in sc_counts.items())
            p(f"- Decision statuses (7d): {pretty}")
        gaps = op.get("trade_sync_gaps") or {}
        if gaps.get("stale"):
            p(f"- ⚠️ **Trade-sync gap**: {len(gaps.get('gaps', []))} run(s) "
              f"submitted orders with no matching fills in the last "
              f"{gaps.get('lookback_days')}d")
        else:
            p("- Trade sync: no gaps detected")

    # --- Cycle timeline -------------------------------------------------
    p("")
    p("## Recent cycles")
    cy = report.get("cycles")
    if isinstance(cy, dict) and cy.get("error"):
        p(f"_unavailable: {cy['error']}_")
    elif cy:
        p("")
        p("| run_id | regime | top_conf | positions | all_cash | sanity | nav_$ | cost_$ |")
        p("|---|---|---|---|---|---|---|---|")
        for r in cy[-15:]:
            p(f"| {r['run_id'][:20]} | {r.get('regime') or '—'} | "
              f"{_fmt(r.get('top_confidence'), '.2f')} | "
              f"{r.get('position_count', 0)} | "
              f"{'Y' if r.get('all_cash') else 'N'} | "
              f"{r.get('sanity_status') or '—'} | "
              f"{_fmt(r.get('nav_usd'), '.2f')} | "
              f"{_fmt(r.get('cost_usd'), '.4f')} |")
    else:
        p("_no runs found under state/runs/_")

    p("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only performance assessment vs CLAUDE.md §Promotion gates.",
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="Most-recent N cycles for the timeline table (default: all).")
    ap.add_argument("--json", type=Path, default=None,
                    help="Also write the machine-readable assessment dict to this path.")
    args = ap.parse_args(argv)

    report = gather(limit=args.limit)
    print(_render(report))

    if args.json:
        args.json.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8",
        )
        print(f"\nWrote machine-readable assessment to {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
