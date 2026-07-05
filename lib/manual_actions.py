"""Operator-initiated broker actions from the dashboard. $0 — no LLM calls.

The dashboard's Portfolio tab exposes a per-position "Close" button; this
module is the testable seam it calls so no broker-mutating code lives inline
in dashboard.py. The close path deliberately mirrors monitor.execute_actions
(flatten → kill event on acceptance only) with two documented divergences:

- halt.flag is NOT checked. The halt flag means "the agents stop trading";
  a human clicking Close IS the manual review the halt exists to enable, and
  risk reduction must stay available while halted (user decision 2026-07-05).
- ORDERS_ENABLED is NOT checked. It gates the orchestrator's portfolio
  convergence (opens/adds) only — the monitor flattens without it, and the
  invariant is "closes are always possible, opens are gated".

Live safety needs no code here: AlpacaBroker's constructor refuses a
non-paper client unless the FULL triple lock is raised, so this path can
never reach a live account under a half-raised lock.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import live_gate, state
from .broker import Broker, OrderResult

MANUAL_CLOSE_REASON = "manual close from dashboard"
MANUAL_CLOSE_EXIT_KIND = "manual_close"


@dataclass(frozen=True)
class ManualCloseResult:
    ok: bool
    symbol: str
    order: OrderResult | None = None
    error: str | None = None
    kill_event_written: bool = False
    sync_error: str | None = None


def close_position_manually(
    symbol: str,
    *,
    broker: Broker | None = None,
    sync_fills: bool = True,
) -> ManualCloseResult:
    """Flatten one position at the operator's request and record why.

    On broker acceptance:
      - appends a kill event (exit_kind=manual_close, source=dashboard) so
        lib.feedback attributes the exit to the operator, not the agent;
      - drops the symbol from state/position_peaks.json — same rationale as
        the monitor's post-flatten peak cleanup: a re-entry must not be
        trailing-stopped against the prior trade's high-water mark;
      - best-effort syncs fills so the closing SELL lands in trades.jsonl
        promptly (engaging the 7-day re-entry cooldown). If the fill isn't
        queryable yet, the orchestrator's pre-cooldown sync catches it
        before the next cycle's cooldown map is computed.

    A rejected/failed flatten (broker.flatten → None) records NOTHING: the
    position is still open, and a phantom kill event could mis-attribute an
    unrelated later close inside the performance memo's 6h match window.
    """
    if broker is None:
        try:
            from .alpaca_client import AlpacaBroker
            broker = AlpacaBroker()
        except Exception as e:
            return ManualCloseResult(
                ok=False, symbol=symbol,
                error=f"broker unavailable: {type(e).__name__}: {e}",
            )

    result = broker.flatten(symbol)
    if result is None:
        return ManualCloseResult(
            ok=False, symbol=symbol,
            error="broker rejected the close (position already closed, "
                  "shares tied up in a pending order, or API failure)",
        )

    kill_event_written = False
    try:
        state.append_kill_event({
            "at": state.utcnow_iso(),
            "symbol": symbol,
            "reason": MANUAL_CLOSE_REASON,
            "exit_kind": MANUAL_CLOSE_EXIT_KIND,
            "source": "dashboard",
            "mode": live_gate.trading_mode(broker),
        })
        kill_event_written = True
    except Exception:
        pass

    # Guarded — telemetry cleanup must never turn an accepted close into an
    # error surfaced to the operator.
    try:
        peaks = state.read_position_peaks()
        if symbol in peaks:
            peaks.pop(symbol, None)
            state.write_position_peaks(peaks)
    except Exception:
        pass

    sync_error: str | None = None
    if sync_fills:
        try:
            from . import trades_sync
            trades_sync.sync_fills_from_alpaca(
                trading_client=getattr(broker, "_client", None),
                order_id_to_run_id=trades_sync.order_id_to_run_id_from_runs(),
                mode=live_gate.trading_mode(broker),
            )
        except Exception as e:
            sync_error = f"{type(e).__name__}: {e}"

    return ManualCloseResult(
        ok=True, symbol=symbol, order=result,
        kill_event_written=kill_event_written, sync_error=sync_error,
    )
