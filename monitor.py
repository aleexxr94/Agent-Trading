"""Lightweight kill-condition checker.

Runs more frequently than the orchestrator (Task Scheduler-driven). Reads
state/current_portfolio.json, evaluates per-position kill conditions and the
8% daily drawdown circuit breaker via lib.risk, and may flatten via the
broker — but cannot open new positions. Halt flag is honoured.
"""
from __future__ import annotations

import argparse
import sys
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
