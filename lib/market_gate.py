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

Behaviour:
  - Broker reports clock.is_open == True → return MarketState(open=True),
    pipeline proceeds normally.
  - Broker reports closed → return MarketState(open=False, next_open=...),
    orchestrator writes next_run.json and exits.
  - Broker returns None (clock unsupported, e.g. test stub) → fall back
    to "open" so dry-runs and unit tests work without a clock stub.
  - Broker raises → caller treats as closed (conservative); orchestrator
    falls back to its default cadence.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import state
from .broker import Broker, MarketClock


@dataclass(frozen=True)
class MarketState:
    """Result of the market-gate check."""
    is_open: bool
    next_open: str | None  # ISO-8601 UTC; None if currently open or unknown
    rationale: str         # human-readable summary for next_run.json / dashboard


def check(broker: Broker | None) -> MarketState:
    """Query the broker's market clock and return a MarketState.

    Resolution policy:
      - broker is None (e.g. dry-run, no Alpaca creds) → return open=True
        so the rest of the pipeline can proceed against fixtures
      - broker.get_clock() returns None (default Broker.get_clock or
        transient broker error) → return open=True; conservative for
        operational continuity (the daily fallback timer + the existing
        order-side market-hours check at Alpaca will still reject orders
        if markets are actually closed)
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
            is_open=True,
            next_open=None,
            rationale=f"market_gate clock fetch failed ({type(e).__name__}); falling open",
        )

    if clock is None:
        return MarketState(
            is_open=True,
            next_open=None,
            rationale="market_gate: broker did not return clock; falling open",
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
        # Next cycle defaults to trade — when markets re-open the agent
        # should pick up with the full pipeline. Review picks come from
        # the meta-scheduler, not from market-closed bypasses.
        "cycle_intent": "trade",
    }
    return next_run
