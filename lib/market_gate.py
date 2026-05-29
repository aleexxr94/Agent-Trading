"""Market-gate stage — v2 pipeline stage 0.

Calls the broker's market clock at the START of every run. If markets are
closed (weekend, holiday, after-hours), the orchestrator short-circuits:
no LLM calls, no signals computation, no orders. Writes a minimal
``next_run.json`` pointing at the next open time and exits cleanly.

This is the cheapest possible reliability + cost win: a closed-market
cycle on the v1 pipeline still paid for screen + research + chains +
scenarios + construct before producing a portfolio that couldn't trade.
v2 just skips when there's nothing tradable.

Cost: $0 — broker clock API is free.

Behaviour (fail-closed in production):
  - broker is None (dry-run / no-broker path) → return MarketState(open=True)
    so dry-runs and unit tests work against fixtures without a clock stub.
    The orchestrator only calls the gate when NOT in dry-run, and dry-run
    passes broker=None.
  - Broker reports clock.is_open == True → return MarketState(open=True),
    pipeline proceeds normally.
  - Broker reports closed → return MarketState(open=False, next_open=...),
    orchestrator writes next_run.json and exits.
  - Broker is present but get_clock() raises OR returns None → FAIL CLOSED:
    return MarketState(open=False, clock_error=True). A real broker is
    expected to implement a working clock; if it can't be reached we must
    not run the full LLM + order pipeline on the assumption that markets are
    open. The orchestrator short-circuits exactly as for a closed market,
    but distinguishes the decision-log status (skipped_clock_error) and
    uses the daily fallback cadence (no next_open is known). The daily
    fallback timer re-runs the cycle later.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import state
from .broker import Broker, MarketClock


@dataclass(frozen=True)
class MarketState:
    """Result of the market-gate check.

    ``clock_error`` distinguishes "the broker clock said the market is
    closed" (``is_open=False, clock_error=False``) from "we couldn't get a
    usable clock and are failing closed" (``is_open=False,
    clock_error=True``). The orchestrator uses it to pick the decision-log
    status and the next-run cadence.
    """
    is_open: bool
    next_open: str | None  # ISO-8601 UTC; None if currently open or unknown
    rationale: str         # human-readable summary for next_run.json / dashboard
    clock_error: bool = False  # True iff the broker clock was unreachable/missing


def check(broker: Broker | None) -> MarketState:
    """Query the broker's market clock and return a MarketState.

    Resolution policy (fail-closed in production):
      - broker is None (e.g. dry-run, no Alpaca creds) → return open=True
        so the rest of the pipeline can proceed against fixtures
      - broker.get_clock() raises (transient API error) → FAIL CLOSED:
        open=False, clock_error=True. We will not run the LLM + order
        pipeline on a stale assumption that markets are open; the daily
        fallback timer re-runs the cycle later.
      - broker.get_clock() returns None (a real broker that can't report a
        clock) → FAIL CLOSED: open=False, clock_error=True. The default
        Broker.get_clock returns None, but a production broker MUST override
        it; a None here means the clock is unavailable, not "assume open".
      - clock.is_open == True → open=True
      - clock.is_open == False → open=False, next_open populated when
        known
    """
    if broker is None:
        return MarketState(
            is_open=True,
            next_open=None,
            rationale="market_gate skipped: no broker available (dry-run path)",
        )

    clock: MarketClock | None
    try:
        clock = broker.get_clock()
    except Exception as e:
        return MarketState(
            is_open=False,
            next_open=None,
            rationale=(
                f"market_gate: broker clock fetch failed ({type(e).__name__}); "
                "failing closed (no LLM calls, no orders)"
            ),
            clock_error=True,
        )

    if clock is None:
        return MarketState(
            is_open=False,
            next_open=None,
            rationale=(
                "market_gate: broker clock unavailable (get_clock returned None); "
                "failing closed (no LLM calls, no orders)"
            ),
            clock_error=True,
        )

    if clock.is_open:
        return MarketState(
            is_open=True,
            next_open=None,
            rationale=(
                f"market open per broker clock (timestamp={clock.timestamp}, "
                f"next_close={clock.next_close})"
            ),
        )

    return MarketState(
        is_open=False,
        next_open=clock.next_open or None,
        rationale=(
            f"market closed per broker clock; next open at {clock.next_open or '<unknown>'} "
            f"(timestamp={clock.timestamp})"
        ),
    )


def write_closed_artifacts(run_id: str, ms: MarketState) -> dict:
    """Write the minimum artifacts a closed-market cycle needs:
    market_gate.json next to the run dir, next_run.json pointing at the
    broker-reported next open (or empty if unknown so the daily fallback
    timer covers us). Returns the next_run dict so the caller can also
    persist it to state.NEXT_RUN.
    """
    gate_payload = {
        "run_id": run_id,
        "generated_at": state.utcnow_iso(),
        "is_open": ms.is_open,
        "next_open": ms.next_open,
        "rationale": ms.rationale,
        "clock_error": ms.clock_error,
    }
    state.write_json(state.run_dir(run_id) / "market_gate.json", gate_payload)

    next_run = {
        "run_id": run_id,
        # Use the broker-reported next-open if available so the scheduler
        # picks up exactly when markets reopen, not 4-6h after via the
        # heuristic fallback. Empty next_run_at means deploy/run_scheduler.sh
        # will skip until the daily fallback timer fires — acceptable but
        # not great; populated next_open is the happy path.
        "next_run_at": ms.next_open or "",
        "rationale": (
            f"stage_execute skipped: {ms.rationale}. "
            "No LLM calls billed; no orders submitted."
        ),
        "market_closed": True,
        # True when we skipped because the broker clock was unreachable/missing
        # rather than a genuine closed market — lets the dashboard flag a
        # broker-connectivity problem distinctly.
        "clock_error": ms.clock_error,
        # Next cycle defaults to trade — when markets re-open the agent
        # should pick up with the full pipeline. Review picks come from
        # the meta-scheduler, not from market-closed bypasses.
        "cycle_intent": "trade",
    }
    return next_run
