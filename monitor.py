"""Lightweight kill-condition checker.

Runs more frequently than the orchestrator (systemd timer-driven on the
Linux VPS — see deploy/systemd/agent-monitor.timer). Reads
state/current_portfolio.json, evaluates per-position kill conditions and the
8% daily drawdown circuit breaker via lib.risk, and may flatten via the
broker — but cannot open new positions. Halt flag is honoured.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

from lib import live_gate
from lib import marks as marks_lib
from lib import risk, state
from lib.broker import Broker

RISK_WARNING = (
    "PAPER TRADING. Monitor.py only flattens losing positions; it never opens. "
    "Not financial advice."
)


def _enforce_stops_enabled() -> bool:
    """Kill-switch for the Phase 1 enforcement (broker-truth price/time
    stops + orphan loss-cap coverage). Default ON. Set
    MONITOR_ENFORCE_STOPS=false to fall back to loss-cap-only behaviour
    without redeploying."""
    return os.environ.get("MONITOR_ENFORCE_STOPS", "true").strip().lower() in ("1", "true", "yes")


def _live_synthetic_nav(*, portfolio: dict, marks: dict, cost_basis: dict) -> float | None:
    """Mark-aware synthetic equity for the drawdown breaker. Returns None on
    any failure so a data problem can never crash the kill loop."""
    try:
        from lib import dashboard_data
        return dashboard_data.live_synthetic_nav(
            marks=marks, portfolio=portfolio, broker_costs=cost_basis,
        )
    except Exception:
        return None


def run_dd_breaker(*, current_nav: float | None, enabled: bool, persist: bool = True) -> dict:
    """Manage the start-of-day baseline + the 8% daily-drawdown halt.

    The first observation of each UTC day sets the baseline. When current
    synthetic NAV is ≥8% below it AND ``enabled``, writes the auto-expiring
    ``dd_halt`` flag the orchestrator reads to skip NEW orders (closes still
    allowed). ``persist=False`` (dry-run) computes without writing state.
    Returns an info dict for the audit/log.
    """
    if current_nav is None:
        return {
            "sod_nav_usd": None, "current_nav_usd": None, "dd_pct": None,
            "tripped": False, "enabled": enabled, "halt_active": state.dd_halt_active(),
        }
    sod = state.read_sod_nav_today()
    if sod is None:
        if persist:
            state.set_sod_nav_today(current_nav)
        sod = current_nav
    tripped, dd = risk.daily_circuit_breaker_tripped(
        sod_nav_usd=sod, current_nav_usd=current_nav,
    )
    if tripped and enabled and persist:
        state.set_dd_halt(dd_pct=dd, sod_nav=sod, current_nav=current_nav)
    return {
        "sod_nav_usd": sod, "current_nav_usd": current_nav, "dd_pct": round(dd, 2),
        "tripped": tripped, "enabled": enabled, "halt_active": state.dd_halt_active(),
    }


def evaluate_portfolio(
    *,
    portfolio: dict,
    marks: dict[str, float],
    cost_basis: dict[str, float] | None = None,
    spots: dict[str, float] | None = None,
    broker_positions: list | None = None,
    now_utc=None,
    enforce_stops: bool = True,
) -> list[dict]:
    """Return flatten action dicts: {symbol, action, reason}.

    ETF-only system. Broker-truth mode (``broker_positions`` provided): a
    position's value and cost basis come from ACTUAL broker holdings — this
    handles partial fills and uses the real ``avg_entry_price`` rather than
    the agent's intended ``avg_cost``. Broker positions the target portfolio
    doesn't name ('orphans') get loss-cap coverage so nothing held goes
    unmonitored. A stray/legacy option position (unsupported instrument) is
    left alone — the system is ETF-only and never opens, sizes, or
    auto-flattens options; it stays visible in the audit orphan list only.

    Legacy mode (``broker_positions is None``): falls back to the target
    portfolio's stored shares/avg_cost. Kept for direct callers/tests.

    ``enforce_stops`` gates the Phase 1 additions (price stops, time stops,
    orphan coverage); the hard loss cap always applies. ``now_utc`` is
    injectable for deterministic time-stop tests. ``spots`` is accepted for
    backwards compatibility but ETF price stops use the ETF's own mark.
    """
    cost_basis = cost_basis or {}
    actions: list[dict] = []
    broker_truth = broker_positions is not None
    broker_by_key = {bp.symbol: bp for bp in (broker_positions or [])}
    covered: set[str] = set()

    for pos in portfolio.get("positions", []):
        symbol = pos["symbol"]
        mark = marks.get(symbol)

        if broker_truth:
            bp = broker_by_key.get(symbol)
            if bp is None or abs(bp.qty) == 0:
                continue  # not held at broker — nothing to flatten
            covered.add(symbol)
            qty = abs(bp.qty)
            basis_per_unit = cost_basis.get(symbol)
            if basis_per_unit is None:
                basis_per_unit = bp.avg_cost
            cost_basis_usd = basis_per_unit * qty
            # Value from the broker's own market_value.
            current_value_usd = abs(bp.market_value)
        else:
            qty = pos["shares"]
            cost_basis_usd = pos["avg_cost"] * qty
            if mark is None:
                continue  # legacy: can't value an unmarked position
            current_value_usd = mark * qty

        # ETF price stops use the ETF's own mark as the spot.
        spot = mark if enforce_stops else None

        kill, reason = risk.should_kill_position(
            current_value_usd=current_value_usd,
            cost_basis_usd=cost_basis_usd,
            extra_kill=pos.get("kill_conditions") if enforce_stops else None,
            spot_price=spot,
            now_utc=now_utc,
        )
        if kill:
            actions.append({"symbol": symbol, "action": "flatten", "reason": reason})

    # Orphan coverage: a broker position the target doesn't name still gets
    # the hard loss cap so nothing held goes unmonitored (Finding 5).
    if broker_truth and enforce_stops:
        for bkey, bp in broker_by_key.items():
            if bkey in covered or abs(bp.qty) == 0:
                continue
            # Unsupported instrument (e.g. a legacy option position): the
            # system is ETF-only and no longer opens, sizes, or auto-flattens
            # options. We deliberately leave such an orphan ALONE — it is not
            # given equity loss-cap coverage and is not flattened. It remains
            # visible via the audit_report orphan list; purging it (if ever
            # wanted) is a manual action, not something monitor decides.
            if bp.asset_class == "us_option":
                continue
            qty = abs(bp.qty)
            kill, reason = risk.should_kill_position(
                current_value_usd=abs(bp.market_value),
                cost_basis_usd=bp.avg_cost * qty,
                extra_kill=None,
                spot_price=None,
                now_utc=now_utc,
            )
            if kill:
                actions.append({
                    "symbol": bkey, "action": "flatten",
                    "reason": f"orphan (not in target portfolio): {reason}",
                })

    return actions


def update_trailing_stops(
    *,
    portfolio: dict,
    marks: dict[str, float],
    broker_positions: list | None = None,
    position_peaks: dict | None = None,
    now_iso: str | None = None,
) -> tuple[dict, list[dict]]:
    """Evaluate the OPTIONAL trailing stops the constructor chose to set.

    Pure function: returns (new_peaks, actions) without touching state —
    main() persists the peaks. For every target position carrying
    ``kill_conditions.trailing_stop_pct``:

      - the peak mark ratchets up: ``peak = max(prior_peak, mark)``
        (initialised at the current mark when first seen, so a freshly
        set stop can never fire on its first observation)
      - the stop fires when ``mark <= peak * (1 - pct/100)``

    Symbols absent from the returned peaks map (position closed, trailing
    stop removed by the constructor, not currently held, or stop fired
    this pass) are dropped from the peak file so a later re-entry starts
    a fresh ratchet. Dropping at fire time (Codex P2, PR #109) matters:
    the peaks are persisted before the flatten executes, so a surviving
    peak could instantly stop out a re-entry against the PRIOR trade's
    high-water mark. Trade-off: if the flatten itself fails, the next
    pass re-seeds the ratchet at the current mark instead of re-firing —
    the hard 25% loss cap (computed from cost basis, not peaks) remains
    the backstop.

    This enforces only what the agent itself chose per position — exactly
    the same contract as the fixed price/time stops.
    """
    peaks = position_peaks or {}
    now_iso = now_iso or state.utcnow_iso()
    held = (
        {bp.symbol for bp in broker_positions if abs(bp.qty) > 0}
        if broker_positions is not None else None
    )
    new_peaks: dict = {}
    actions: list[dict] = []
    for pos in portfolio.get("positions", []):
        symbol = pos.get("symbol")
        kc = pos.get("kill_conditions") or {}
        pct = kc.get("trailing_stop_pct")
        if not isinstance(pct, (int, float)) or pct <= 0:
            continue
        if held is not None and symbol not in held:
            continue  # not held — no ratchet to maintain
        mark = marks.get(symbol)
        if mark is None or mark <= 0:
            # Unmarked this cycle: keep the prior peak (don't lose the
            # ratchet to a transient data gap), but can't evaluate.
            if symbol in peaks:
                new_peaks[symbol] = peaks[symbol]
            continue
        prior = (peaks.get(symbol) or {}).get("peak_mark")
        peak = max(float(prior), mark) if isinstance(prior, (int, float)) else mark
        new_peaks[symbol] = {"peak_mark": peak, "updated_at": now_iso}
        threshold = peak * (1.0 - float(pct) / 100.0)
        if mark <= threshold and peak > mark:
            new_peaks.pop(symbol, None)
            actions.append({
                "symbol": symbol,
                "action": "flatten",
                "reason": (
                    f"trailing stop: mark {mark:g} ≤ {threshold:g} "
                    f"(peak {peak:g} − {pct:g}%)"
                ),
            })
    return new_peaks, actions


def _exit_kind_from_reason(reason: str) -> str:
    """Map a should_kill_position reason string to a stable exit_kind tag
    for the kill-event log (consumed by lib.feedback + the dashboard)."""
    r = (reason or "").lower()
    if "trailing stop" in r:
        return "trailing_stop"
    if "time stop" in r:
        return "time_stop"
    if "kill_below" in r or "kill_above" in r:
        return "price_stop"
    if r.startswith("orphan"):
        return "orphan_loss_cap"
    if "cap" in r:
        return "loss_cap"
    return "other"


def execute_actions(actions: list[dict], *, broker: Broker | None) -> None:
    if state.is_halted():
        return
    for a in actions:
        if a["action"] == "flatten" and broker is not None:
            broker.flatten(a["symbol"])
            # Exit-outcome audit: record WHY this position died so the
            # performance memo can show the agent its stop-out history.
            # Guarded — telemetry must never break the kill loop.
            try:
                state.append_kill_event({
                    "at": state.utcnow_iso(),
                    "symbol": a["symbol"],
                    "reason": a["reason"],
                    "exit_kind": _exit_kind_from_reason(a["reason"]),
                    "source": "monitor",
                })
            except Exception:
                pass


def audit_report(
    *,
    portfolio: dict,
    broker_positions: list,
    marks: dict[str, float],
    actions: list[dict],
    enforce_stops: bool,
    dd_info: dict | None = None,
) -> dict:
    """Per-cycle monitor audit appended to state/monitor_shadow.jsonl.

    Records coverage vs broker truth (orphans / missing / unmarked) and the
    flatten actions that actually fired this cycle. Reuses the already-fetched
    ``broker_positions`` so it adds no extra broker round trip. Observability
    only — nothing reads this to gate orders.
    """
    positions = portfolio.get("positions") or []
    broker_syms = {p.symbol for p in broker_positions}

    expected: set[str] = set()
    for pos in positions:
        if pos.get("symbol"):
            expected.add(pos["symbol"])

    unmarked: list[dict] = []
    for pos in positions:
        symbol = pos.get("symbol")
        if marks.get(symbol) is None:
            unmarked.append({"symbol": symbol, "kind": pos.get("kind"), "mark_key": symbol})

    # Prefer the live (Phase 2) breaker state when the caller computed it;
    # fall back to a nav_history (cycle-granularity) proxy otherwise.
    if dd_info is not None:
        daily_dd = {
            "sod_nav_usd": dd_info.get("sod_nav_usd"),
            "current_nav_usd": dd_info.get("current_nav_usd"),
            "dd_pct": dd_info.get("dd_pct"),
            "halt_active": dd_info.get("halt_active", False),
            "enabled": dd_info.get("enabled"),
            "source": "live_synthetic_nav",
        }
    else:
        rows = state.read_nav_history(limit=1000)
        today = state.utcnow().date().isoformat()
        todays = [r for r in rows if str(r.get("at") or "").startswith(today)]
        sod_nav = todays[0].get("nav_usd") if todays else None
        ref_nav = rows[-1].get("nav_usd") if rows else None
        dd_pct: float | None = None
        dd_would_halt = False
        if sod_nav is not None and ref_nav is not None and float(sod_nav) > 0:
            dd_would_halt, dd_pct = risk.daily_circuit_breaker_tripped(
                sod_nav_usd=float(sod_nav), current_nav_usd=float(ref_nav),
            )
        daily_dd = {
            "sod_nav_usd": sod_nav,
            "ref_nav_usd": ref_nav,
            "dd_pct": round(dd_pct, 2) if dd_pct is not None else None,
            "would_halt_new_orders": dd_would_halt,
            "source": "nav_history_proxy",
        }

    return {
        "at": state.utcnow_iso(),
        "enforce_stops": enforce_stops,
        "coverage": {
            "portfolio_positions": len(positions),
            "broker_positions": len(broker_positions),
            "unmarked": len(unmarked),
            "unmarked_detail": unmarked,
            "orphans": sorted(broker_syms - expected),
            "missing": sorted(expected - broker_syms),
        },
        "fired": [{"symbol": a["symbol"], "reason": a["reason"]} for a in actions],
        "daily_dd_shadow": daily_dd,
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

    # Same triple-lock guard as the orchestrator: monitor can flatten/cancel,
    # so it must also refuse to run under a half-raised live gate (fail closed).
    gate_exit = live_gate.assert_live_gate(entrypoint="monitor")
    if gate_exit is not None:
        return gate_exit

    if state.is_halted():
        print("halt.flag set; nothing to do.")
        return 0

    if not state.CURRENT_PORTFOLIO.exists():
        print("No current_portfolio.json yet; nothing to monitor.")
        return 0

    portfolio = state.read_json(state.CURRENT_PORTFOLIO)
    broker = _try_load_broker()
    # One broker round trip; reuse the positions for marks, cost basis,
    # evaluation, and the audit so we never fetch twice.
    positions: list = []
    if broker is not None:
        try:
            positions = broker.get_positions()
        except Exception as e:
            print(f"monitor: get_positions failed ({type(e).__name__}: {e}); evaluating with no marks")
            positions = []
    marks = marks_lib.marks_from_positions(positions)
    cost_basis = marks_lib.cost_basis_from_positions(positions)
    enforce = _enforce_stops_enabled()
    # ETF price stops use the ETF's own mark as the spot — no separate
    # underlying-price fetch is needed.
    actions = evaluate_portfolio(
        portfolio=portfolio,
        marks=marks,
        cost_basis=cost_basis,
        broker_positions=(positions if broker is not None else None),
        enforce_stops=enforce,
    )
    # Trailing stops the constructor chose (ratchet from peak mark).
    # Guarded like the other telemetry: a peak-file problem degrades the
    # ratchet, it never breaks the hard loss-cap loop above.
    if enforce:
        try:
            new_peaks, trailing_actions = update_trailing_stops(
                portfolio=portfolio,
                marks=marks,
                broker_positions=(positions if broker is not None else None),
                position_peaks=state.read_position_peaks(),
            )
            already = {a["symbol"] for a in actions}
            actions.extend(
                a for a in trailing_actions if a["symbol"] not in already
            )
            # ANY flatten this pass (loss cap, price/time stop, orphan,
            # or trailing) invalidates the symbol's ratchet — peaks are
            # persisted before execute_actions() runs, so a surviving
            # peak could stop a re-entry against the prior trade's
            # high-water mark (Codex P2 follow-up, PR #109: the
            # fire-time drop inside update_trailing_stops only covers
            # trailing-stop exits, not the other flatten rules).
            for a in actions:
                if a.get("action") == "flatten":
                    new_peaks.pop(a.get("symbol"), None)
            if not args.dry_run:
                state.write_position_peaks(new_peaks)
        except Exception as e:
            print(f"monitor: trailing-stop error ({type(e).__name__}: {e}); ignored")
    print(
        f"monitor: {len(marks)} marks, {len(actions)} actions "
        f"(dry_run={args.dry_run}, broker={'on' if broker else 'off'}, "
        f"enforce_stops={enforce})"
    )
    if not args.dry_run:
        execute_actions(actions, broker=broker)
    # Phase 2: 8% daily-drawdown breaker. Computes live synthetic equity from
    # the positions already fetched, manages the start-of-day baseline, and
    # writes the auto-expiring dd_halt flag the orchestrator reads to skip new
    # orders. Guarded so a data problem can never break the kill loop.
    dd_info = None
    try:
        dd_enabled = risk.dd_breaker_enabled()
        current_nav = _live_synthetic_nav(portfolio=portfolio, marks=marks, cost_basis=cost_basis)
        dd_info = run_dd_breaker(
            current_nav=current_nav, enabled=dd_enabled, persist=not args.dry_run,
        )
        if dd_info.get("tripped"):
            print(
                f"monitor: DAILY DD BREAKER dd={dd_info['dd_pct']}% ≥ 8% "
                f"(enabled={dd_info['enabled']}) — new orders halted for the UTC day"
            )
    except Exception as e:
        print(f"monitor: dd-breaker error ({type(e).__name__}: {e}); ignored")
    # Per-cycle audit — reuses the already-fetched positions (no extra round
    # trip). Fully guarded so a telemetry bug can never break the kill loop.
    try:
        report = audit_report(
            portfolio=portfolio, broker_positions=positions, marks=marks,
            actions=actions, enforce_stops=enforce, dd_info=dd_info,
        )
        state.append_monitor_shadow(report)
        cov = report["coverage"]
        print(
            f"monitor-audit: tracked={cov['portfolio_positions']} "
            f"held={cov['broker_positions']} unmarked={cov['unmarked']} "
            f"orphans={len(cov['orphans'])} missing={len(cov['missing'])}; "
            f"fired={len(report['fired'])}"
        )
    except Exception as e:
        print(f"monitor-audit: telemetry error ({type(e).__name__}: {e}); ignored")
    # Trade-sync staleness: orders were accepted recently but no fills ever
    # synced — the failure mode that once silently broke cooldown/P&L for
    # 5 weeks. Loud in the journal; the dashboard shows the same banner.
    try:
        from lib import dashboard_data as _dd
        sync = _dd.trade_sync_gaps()
        if sync.get("stale"):
            print(
                f"monitor: WARNING trade-sync staleness — {len(sync['gaps'])} "
                f"run(s) submitted accepted orders in the last "
                f"{sync['lookback_days']}d with no matching fills in "
                f"trades.jsonl (cooldown/P&L degraded)"
            )
    except Exception as e:
        print(f"monitor: sync-staleness check error ({type(e).__name__}: {e}); ignored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
