#!/usr/bin/env python3
"""Post-hoc cycle-by-cycle analysis tool.

Walks ``state/runs/*``, extracts (view, portfolio, sanity, next_run)
per cycle, joins against ``state/nav_history.jsonl`` to compute
realized cycle-over-cycle NAV change, and emits a summary table.

This is the v2 substitute for a full LLM-replay backtest. Real
backtesting needs paid Alpaca historical market data; this tool just
analyzes what actually happened on past cycles. Use it to:

  - Spot drift: is the strategist consistently calling `risk_on`
    in periods that lost money?
  - Spot constructor patterns: which positions keep showing up
    cycle after cycle (anchor bias)?
  - Sanity audit: how often did sanity warn / fail?
  - LLM cost trend: per-cycle cost over time.

Run:
    python -m bin.analyze_runs [--limit N] [--csv path]

Outputs to stdout as a markdown table by default; --csv writes a
machine-readable file.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import state  # noqa: E402


def _load_run(run_dir: Path) -> dict:
    """Return a compact summary dict for one run."""
    rid = run_dir.name
    summary: dict = {
        "run_id": rid,
        "regime": None,
        "candidates_count": 0,
        "top_confidence": None,
        "position_count": 0,
        "all_cash": None,
        "construction_rationale_len": 0,
        "sanity_status": None,
        "sanity_fail_count": 0,
        "next_run_at": None,
        "dedup_skipped": False,
        "market_closed": False,
    }
    view_path = run_dir / "view.json"
    if view_path.exists():
        try:
            view = json.loads(view_path.read_text())
            summary["regime"] = view.get("regime")
            summary["candidates_count"] = len(view.get("candidates", []))
            confs = [c.get("confidence") for c in view.get("candidates", [])
                     if isinstance(c.get("confidence"), (int, float))]
            summary["top_confidence"] = max(confs) if confs else None
        except (json.JSONDecodeError, OSError):
            pass
    p_path = run_dir / "portfolio.json"
    if p_path.exists():
        try:
            p = json.loads(p_path.read_text())
            summary["position_count"] = len(p.get("positions", []))
            summary["all_cash"] = p.get("all_cash", False)
            summary["construction_rationale_len"] = len(p.get("construction_rationale", ""))
        except (json.JSONDecodeError, OSError):
            pass
    s_path = run_dir / "sanity.json"
    if s_path.exists():
        try:
            s = json.loads(s_path.read_text())
            summary["sanity_status"] = s.get("status")
            summary["sanity_fail_count"] = s.get("summary", {}).get("fail", 0)
        except (json.JSONDecodeError, OSError):
            pass
    nr_path = run_dir / "next_run.json"
    if nr_path.exists():
        try:
            nr = json.loads(nr_path.read_text())
            summary["next_run_at"] = nr.get("next_run_at")
            summary["dedup_skipped"] = bool(nr.get("dedup_skipped"))
            summary["market_closed"] = bool(nr.get("market_closed"))
        except (json.JSONDecodeError, OSError):
            pass
    return summary


def _attach_realized_pnl(rows: list[dict]) -> list[dict]:
    """Look up each run's realized 4h NAV change in nav_history.jsonl."""
    nav_rows = state.read_nav_history(limit=10_000)
    nav_by_run = {r.get("run_id"): r.get("nav_usd") for r in nav_rows if r.get("run_id")}
    # Sort runs by their next_run_at proxy or run_id (chronological).
    sorted_runs = sorted(rows, key=lambda r: r["run_id"])
    prev_nav: float | None = None
    for r in sorted_runs:
        curr_nav = nav_by_run.get(r["run_id"])
        if curr_nav is None or prev_nav is None or prev_nav <= 0:
            r["nav_usd"] = curr_nav
            r["realized_pnl_pct"] = None
        else:
            r["nav_usd"] = curr_nav
            r["realized_pnl_pct"] = round((curr_nav / prev_nav - 1.0) * 100.0, 4)
        if curr_nav is not None:
            prev_nav = curr_nav
    return sorted_runs


def _per_run_cost(run_id: str) -> float:
    """Sum cost_usd from state/costs.jsonl for this run_id."""
    total = 0.0
    for row in state.read_costs_for_run(run_id):
        total += float(row.get("cost_usd") or 0.0)
    return total


def collect(limit: int | None = None) -> list[dict]:
    """Walk state/runs/ and return summarized rows, oldest first."""
    if not state.RUNS_DIR.exists():
        return []
    run_dirs = sorted(
        (d for d in state.RUNS_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    if limit is not None:
        run_dirs = run_dirs[-limit:]
    rows = [_load_run(d) for d in run_dirs]
    rows = _attach_realized_pnl(rows)
    for r in rows:
        r["cost_usd"] = round(_per_run_cost(r["run_id"]), 4)
    return rows


def _print_markdown(rows: list[dict]) -> None:
    header = (
        "| run_id | regime | top_conf | positions | all_cash | "
        "sanity | nav_usd | realized_pnl_% | cost_$ |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|"
    print(header)
    print(sep)
    for r in rows:
        print(
            f"| {r['run_id'][:26]} "
            f"| {r.get('regime') or '—'} "
            f"| {('%.2f' % r['top_confidence']) if r.get('top_confidence') else '—'} "
            f"| {r.get('position_count', 0)} "
            f"| {'Y' if r.get('all_cash') else 'N'} "
            f"| {r.get('sanity_status') or '—'} "
            f"| {('%.2f' % r['nav_usd']) if r.get('nav_usd') else '—'} "
            f"| {('%+.3f' % r['realized_pnl_pct']) if r.get('realized_pnl_pct') is not None else '—'} "
            f"| {('%.4f' % r['cost_usd']) if r.get('cost_usd') else '—'} |"
        )


def _print_aggregate(rows: list[dict]) -> None:
    if not rows:
        return
    total_cycles = len(rows)
    realized = [r["realized_pnl_pct"] for r in rows if r.get("realized_pnl_pct") is not None]
    win_rate = (sum(1 for x in realized if x > 0) / len(realized) * 100.0) if realized else None
    total_pnl = sum(realized) if realized else 0.0
    total_cost = sum(r.get("cost_usd") or 0.0 for r in rows)
    by_regime: dict[str, list[float]] = {}
    for r in rows:
        if r.get("realized_pnl_pct") is None:
            continue
        by_regime.setdefault(r.get("regime") or "—", []).append(r["realized_pnl_pct"])
    print()
    print("### Aggregate")
    print(f"- cycles: {total_cycles}")
    print(f"- cycles with realized PnL: {len(realized)}")
    if win_rate is not None:
        print(f"- win rate: {win_rate:.1f}%")
        print(f"- mean realized PnL/cycle: {(sum(realized) / len(realized)):+.3f}%")
        print(f"- cumulative realized PnL: {total_pnl:+.3f}%")
    print(f"- total LLM cost: ${total_cost:.2f}")
    if by_regime:
        print("- PnL by regime:")
        for regime, pnls in sorted(by_regime.items()):
            print(f"  - {regime}: n={len(pnls)}, "
                  f"mean={(sum(pnls) / len(pnls)):+.3f}%, "
                  f"cum={sum(pnls):+.3f}%")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Post-hoc analysis of state/runs/")
    p.add_argument("--limit", type=int, default=None,
                   help="Most-recent N runs only (default: all).")
    p.add_argument("--csv", type=Path, default=None,
                   help="Write to CSV instead of markdown table.")
    args = p.parse_args(argv)
    rows = collect(limit=args.limit)
    if not rows:
        print("No runs found under state/runs/", file=sys.stderr)
        return 1
    if args.csv:
        fields = list(rows[0].keys())
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}", file=sys.stderr)
    else:
        _print_markdown(rows)
        _print_aggregate(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
