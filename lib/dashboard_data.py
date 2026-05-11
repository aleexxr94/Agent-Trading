"""Pure data-layer helpers for the dashboard.

Separated from dashboard.py so they can be unit-tested without streamlit
installed. Keep this file streamlit-free.
"""
from __future__ import annotations

import json
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
    return rows[-limit:]


def cost_today_usd() -> float:
    return sum(r.get("cost_usd", 0.0) for r in state.read_costs_today())


def cost_for_run_usd(run_id: str) -> float:
    return sum(r.get("cost_usd", 0.0) for r in state.read_costs_for_run(run_id))


def total_token_cost() -> dict:
    """All-time totals across this project's state/costs.jsonl. Project-scoped
    (the SDK only writes to this file from this codebase)."""
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


def latest_run_id() -> str | None:
    if not state.DECISIONS_LOG.exists():
        return None
    rows = load_decisions(limit=10_000)
    return rows[-1]["run_id"] if rows else None


def position_table_rows(
    portfolio: dict, marks: dict[str, float] | None = None,
) -> list[dict]:
    """Flatten ETF + option rows into uniform columns for st.dataframe.

    When `marks` is provided (keys: ETF symbol, or
    f"{underlying}|{strike}|{expiry}|{type}" for options — same convention as
    monitor.py / compute_portfolio_pnl), each row gets Mark / Gross P&L /
    Net P&L columns. Without marks those columns stay '—'.
    """
    marks = marks or {}
    out: list[dict] = []
    for p in portfolio.get("positions", []):
        if p["kind"] == "etf":
            shares = p["shares"]
            mark = marks.get(p["symbol"])
            row = {
                "Symbol": p["symbol"],
                "Kind": "ETF",
                "Leverage": f"{p.get('leverage_factor', 1):g}x",
                "Qty": shares,
                "Cost": p["avg_cost"],
                "Notional": shares * p["avg_cost"],
                "% NAV": p["position_pct"],
                "Greeks": "—",
                "Kill": f"≤{p['kill_conditions']['max_loss_pct']}%",
            }
        else:
            contracts = p["contracts"]
            premium_usd = p["premium_paid"] * 100 * contracts
            g = p["greeks"]
            # marks_from_broker currently keys options by OSI symbol; the
            # rest of the codebase uses the synthetic underlying|strike|expiry|type
            # key. Try the synthetic key first (forward-compatible with the
            # PR-#20 alignment fix), then fall back to the OSI key so live
            # marks match today on main.
            synth_key = f"{p['underlying']}|{p['strike']}|{p['expiry']}|{p['type']}"
            mark = marks.get(synth_key)
            if mark is None:
                try:
                    osi = osi_symbol(
                        underlying=p["underlying"], expiry=p["expiry"],
                        type=p["type"], strike=p["strike"],
                    )
                    mark = marks.get(osi)
                except (ValueError, KeyError):
                    pass
            row = {
                "Symbol": f"{p['underlying']} {p['type'].upper()} {p['strike']} {p['expiry']}",
                "Kind": "OPT",
                "Leverage": "—",
                "Qty": contracts,
                "Cost": p["premium_paid"],
                "Notional": premium_usd,
                "% NAV": p["position_pct"],
                "Greeks": (
                    f"Δ{g['delta']:.2f} Θ{g['theta']:.2f} "
                    f"IV {g['iv']*100:.0f}% (p{int(g['iv_percentile'])})"
                ),
                "Kill": f"≤{p['kill_conditions']['max_loss_pct']}%",
            }
        breakdown = pnl_lib.compute_position_pnl(position=p, current_mark_usd=mark)
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
