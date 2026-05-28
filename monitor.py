"""Lightweight kill-condition checker.

Runs more frequently than the orchestrator (Task Scheduler-driven). Reads
state/current_portfolio.json, evaluates per-position kill conditions and the
8% daily drawdown circuit breaker via lib.risk, and may flatten via the
broker — but cannot open new positions. Halt flag is honoured.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

from lib import marks as marks_lib
from lib import risk, state
from lib.broker import Broker

RISK_WARNING = (
    "PAPER TRADING. Monitor.py only flattens losing positions; it never opens. "
    "Not financial advice."
)


def evaluate_portfolio(
    *,
    portfolio: dict,
    marks: dict[str, float],
    spots: dict[str, float] | None = None,
    sod_nav_usd: float | None = None,
) -> list[dict]:
    """Return a list of action dicts: {symbol, action, reason}."""
    actions: list[dict] = []
    spots = spots or {}

    nav = sum(p["position_pct"] for p in portfolio.get("positions", []))
    current_nav = portfolio.get("nav_usd", 0.0)
    if sod_nav_usd is not None:
        tripped, dd = risk.daily_circuit_breaker_tripped(
            sod_nav_usd=sod_nav_usd, current_nav_usd=current_nav,
        )
        if tripped:
            actions.append({
                "symbol": "*",
                "action": "halt_new_orders",
                "reason": f"daily DD {dd:.1f}% ≥ 8%",
            })

    for pos in portfolio.get("positions", []):
        is_option = pos["kind"] == "option"
        symbol = pos.get("underlying") if is_option else pos["symbol"]
        mark_key = symbol if not is_option else f"{symbol}|{pos.get('strike')}|{pos.get('expiry')}|{pos.get('type')}"
        mark = marks.get(mark_key)
        if mark is None:
            continue
        if is_option:
            current_value = mark * pos["contracts"] * 100
            cost_basis = pos["premium_paid"] * pos["contracts"] * 100
        else:
            current_value = mark * pos["shares"]
            cost_basis = pos["avg_cost"] * pos["shares"]
        kill, reason = risk.should_kill_position(
            current_value_usd=current_value,
            cost_basis_usd=cost_basis,
            is_option=is_option,
            extra_kill=pos.get("kill_conditions"),
            spot_price=spots.get(symbol),
        )
        if kill:
            # For options, flatten by the OCC OSI symbol — broker.flatten('SPY')
            # for a SPY call would try to close the ETF position, not the
            # specific contract. Build the OSI from the position fields.
            if is_option:
                from lib.orders import osi_symbol
                flatten_sym = osi_symbol(
                    underlying=pos["underlying"], expiry=pos["expiry"],
                    type=pos["type"], strike=pos["strike"],
                )
            else:
                flatten_sym = symbol
            actions.append({"symbol": flatten_sym, "action": "flatten", "reason": reason})

    return actions


def execute_actions(actions: list[dict], *, broker: Broker | None) -> None:
    if state.is_halted():
        return
    for a in actions:
        if a["action"] == "flatten" and broker is not None:
            broker.flatten(a["symbol"])
        # halt_new_orders is observed by the orchestrator at next start


def _parse_iso_utc(s: str | None) -> datetime | None:
    """Tolerant ISO-8601 → aware-UTC parse. Returns None on anything unparseable."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def shadow_report(*, portfolio: dict, broker: Broker | None, marks: dict[str, float]) -> dict:
    """Phase 0 shadow telemetry — PURELY OBSERVATIONAL.

    Computes what the currently-inert controls (per-position price stops,
    time stops, and the 8% daily-DD breaker) WOULD do this cycle, plus
    monitor coverage vs broker truth (orphaned / unmarked / missing
    positions). Takes NO action: never flattens, never gates orders, never
    writes a halt flag. The real kill path in ``evaluate_portfolio`` is
    untouched. Returns a dict the caller appends to state/monitor_shadow.jsonl.
    """
    now = state.utcnow()
    positions = portfolio.get("positions") or []

    broker_positions: list = []
    if broker is not None:
        try:
            broker_positions = broker.get_positions()
        except Exception:
            broker_positions = []
    broker_syms = {p.symbol for p in broker_positions}

    # Expected broker symbols implied by the target portfolio, so we can
    # surface target-vs-broker drift (finding 5).
    expected: set[str] = set()
    for pos in positions:
        if pos.get("kind") == "option":
            try:
                from lib.orders import osi_symbol
                expected.add(osi_symbol(
                    underlying=pos["underlying"], expiry=pos["expiry"],
                    type=pos["type"], strike=pos["strike"],
                ))
            except Exception:
                pass
        elif pos.get("symbol"):
            expected.add(pos["symbol"])

    would_fire: list[dict] = []
    unmarked: list[dict] = []
    for pos in positions:
        is_option = pos.get("kind") == "option"
        symbol = pos.get("underlying") if is_option else pos.get("symbol")
        mark_key = symbol if not is_option else (
            f"{symbol}|{pos.get('strike')}|{pos.get('expiry')}|{pos.get('type')}"
        )
        if marks.get(mark_key) is None:
            unmarked.append({"symbol": symbol, "kind": pos.get("kind"), "mark_key": mark_key})

        kc = pos.get("kill_conditions") or {}
        ts = _parse_iso_utc(kc.get("time_stop_utc"))
        if ts is not None and now >= ts:
            would_fire.append({
                "symbol": symbol, "kind": pos.get("kind"), "rule": "time_stop_utc",
                "detail": f"time_stop {kc.get('time_stop_utc')} passed", "enforced": False,
            })
        below, above = kc.get("underlying_price_below"), kc.get("underlying_price_above")
        # For an ETF the mark IS the per-share spot (marks prefers the broker's
        # current_price, falling back to market_value/qty), so use it directly —
        # a missing current_price field must not drop the price-stop shadow.
        spot = marks.get(mark_key) if not is_option else None
        if spot is not None:
            if below is not None and spot <= below:
                would_fire.append({"symbol": symbol, "kind": "etf", "rule": "underlying_price_below",
                                   "detail": f"spot {spot} <= {below}", "enforced": False})
            if above is not None and spot >= above:
                would_fire.append({"symbol": symbol, "kind": "etf", "rule": "underlying_price_above",
                                   "detail": f"spot {spot} >= {above}", "enforced": False})

    # Daily-DD shadow at nav_history (cycle) granularity, same units on both
    # sides to avoid the broker($100k)-vs-synthetic($2.5k) mismatch. A live
    # intra-day version lands in Phase 2/3.
    rows = state.read_nav_history(limit=1000)
    today = now.date().isoformat()
    todays = [r for r in rows if str(r.get("at") or "").startswith(today)]
    sod_nav = todays[0].get("nav_usd") if todays else None
    ref_nav = rows[-1].get("nav_usd") if rows else None
    dd_pct: float | None = None
    dd_would_halt = False
    # ``is not None`` (not truthiness): a total wipeout where ref_nav == 0.0 is
    # a 100% drawdown the breaker should flag, not a row to skip (Codex P2).
    if sod_nav is not None and ref_nav is not None and float(sod_nav) > 0:
        dd_would_halt, dd_pct = risk.daily_circuit_breaker_tripped(
            sod_nav_usd=float(sod_nav), current_nav_usd=float(ref_nav),
        )

    return {
        "at": state.utcnow_iso(),
        "coverage": {
            "portfolio_positions": len(positions),
            "broker_positions": len(broker_positions),
            "unmarked": len(unmarked),
            "unmarked_detail": unmarked,
            "orphans": sorted(broker_syms - expected),
            "missing": sorted(expected - broker_syms),
        },
        "would_fire": would_fire,
        "daily_dd_shadow": {
            "sod_nav_usd": sod_nav,
            "ref_nav_usd": ref_nav,
            "dd_pct": round(dd_pct, 2) if dd_pct is not None else None,
            "would_halt_new_orders": dd_would_halt,
            "note": "nav_history proxy at cycle granularity; live intra-day version is Phase 2/3",
        },
        "note": (
            "PHASE 0 SHADOW TELEMETRY — observational only; no action taken, "
            "no orders gated, nothing flattened"
        ),
    }


def _try_load_broker() -> Broker | None:
    """Best-effort AlpacaBroker construction. Returns None if creds are
    missing or the SDK isn't installed — monitor still runs (just can't
    fetch live marks or flatten)."""
    try:
        from lib.alpaca_client import AlpacaBroker
        return AlpacaBroker()
    except Exception as e:
        print(f"broker unavailable ({type(e).__name__}: {e}); monitor will skip mark-based checks")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kill-condition monitor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if state.is_halted():
        print("halt.flag set; nothing to do.")
        return 0

    if not state.CURRENT_PORTFOLIO.exists():
        print("No current_portfolio.json yet; nothing to monitor.")
        return 0

    portfolio = state.read_json(state.CURRENT_PORTFOLIO)
    broker = _try_load_broker()
    marks = marks_lib.marks_from_broker(broker) if broker is not None else {}
    actions = evaluate_portfolio(portfolio=portfolio, marks=marks)
    print(
        f"monitor: {len(marks)} marks, {len(actions)} actions "
        f"(dry_run={args.dry_run}, broker={'on' if broker else 'off'})"
    )
    if not args.dry_run:
        execute_actions(actions, broker=broker)
    # Phase 0 shadow telemetry — runs AFTER real risk actions so its extra
    # broker round-trip can never delay a loss-cap flatten (Codex P1). Fully
    # guarded so an observability bug can never take down the real kill loop.
    try:
        report = shadow_report(portfolio=portfolio, broker=broker, marks=marks)
        state.append_monitor_shadow(report)
        cov, wf = report["coverage"], report["would_fire"]
        tags = ",".join(f"{e['symbol']}:{e['rule']}" for e in wf) or "none"
        print(
            f"monitor-shadow: tracked={cov['portfolio_positions']} "
            f"held={cov['broker_positions']} unmarked={cov['unmarked']} "
            f"orphans={len(cov['orphans'])} missing={len(cov['missing'])}; "
            f"would_fire={len(wf)} [{tags}]"
        )
    except Exception as e:
        print(f"monitor-shadow: telemetry error ({type(e).__name__}: {e}); ignored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
