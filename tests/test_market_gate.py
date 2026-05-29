"""Tests for lib/market_gate — v2 pipeline stage 0.

The gate's job is to short-circuit the pipeline when markets are
closed (weekend, holiday, after-hours) so the orchestrator doesn't
burn LLM calls producing a portfolio that can't trade.
"""
from __future__ import annotations

import json

import pytest

from lib import market_gate, state
from lib.broker import Broker, MarketClock


class _StubBroker(Broker):
    """Minimal Broker stub. Tests overwrite get_clock per-case."""
    def __init__(self, *, clock=None, clock_raises=None):
        self._clock = clock
        self._clock_raises = clock_raises

    @property
    def name(self) -> str:
        return "stub"

    def get_account(self): ...
    def get_positions(self): ...
    def submit_order(self, order): ...
    def cancel_all(self) -> int: return 0
    def flatten(self, symbol): return None

    def get_clock(self):
        if self._clock_raises is not None:
            raise self._clock_raises
        return self._clock


def test_check_returns_open_when_broker_is_none():
    """No broker (dry-run path) → fall open so the rest of the pipeline
    can proceed against fixtures."""
    ms = market_gate.check(None)
    assert ms.is_open is True
    assert ms.next_open is None
    assert "no broker" in ms.rationale.lower()


def test_check_returns_closed_when_broker_does_not_implement_clock():
    """A present broker that can't report a clock (get_clock → None) FAILS
    CLOSED in production: we don't run the LLM + order pipeline on the
    assumption that markets are open. clock_error marks it as a
    connectivity problem rather than a genuine closed market."""
    ms = market_gate.check(_StubBroker(clock=None))
    assert ms.is_open is False
    assert ms.clock_error is True
    assert ms.next_open is None
    assert "unavailable" in ms.rationale.lower()


def test_check_returns_closed_when_broker_raises():
    """Transient API error → FAIL CLOSED. We will not burn LLM calls or
    submit orders on a stale assumption that markets are open; the daily
    fallback timer re-runs the cycle later."""
    ms = market_gate.check(_StubBroker(clock_raises=RuntimeError("network glitch")))
    assert ms.is_open is False
    assert ms.clock_error is True
    assert ms.next_open is None
    assert "RuntimeError" in ms.rationale


def test_check_returns_open_when_clock_reports_open():
    clock = MarketClock(
        is_open=True,
        next_open="",
        next_close="2026-05-13T20:00:00Z",
        timestamp="2026-05-13T14:00:00Z",
    )
    ms = market_gate.check(_StubBroker(clock=clock))
    assert ms.is_open is True
    assert ms.next_open is None
    assert "market open" in ms.rationale.lower()


def test_check_returns_closed_when_clock_reports_closed():
    clock = MarketClock(
        is_open=False,
        next_open="2026-05-14T13:30:00Z",
        next_close="",
        timestamp="2026-05-13T22:00:00Z",
    )
    ms = market_gate.check(_StubBroker(clock=clock))
    assert ms.is_open is False
    assert ms.clock_error is False  # genuine closed market, not a clock error
    assert ms.next_open == "2026-05-14T13:30:00Z"
    assert "market closed" in ms.rationale.lower()


def test_check_returns_closed_with_none_next_open_when_unknown():
    """Broker reports closed but doesn't know the next-open time —
    rare but possible (extended-hours close, holiday). Don't crash."""
    clock = MarketClock(
        is_open=False, next_open="", next_close="", timestamp="2026-05-13T22:00:00Z",
    )
    ms = market_gate.check(_StubBroker(clock=clock))
    assert ms.is_open is False
    assert ms.next_open is None


def test_write_closed_artifacts_creates_market_gate_and_next_run(tmp_state):
    """When the gate is closed, write_closed_artifacts writes
    market_gate.json next to the run dir and returns a next_run dict
    with next_run_at set to the broker-reported next open."""
    rid = state.new_run_id()
    ms = market_gate.MarketState(
        is_open=False,
        next_open="2026-05-14T13:30:00Z",
        rationale="test: market closed; next open Wednesday",
    )
    next_run = market_gate.write_closed_artifacts(rid, ms)

    gate_path = state.run_dir(rid) / "market_gate.json"
    assert gate_path.exists()
    gate = json.loads(gate_path.read_text())
    assert gate["run_id"] == rid
    assert gate["is_open"] is False
    assert gate["next_open"] == "2026-05-14T13:30:00Z"

    assert next_run["run_id"] == rid
    assert next_run["next_run_at"] == "2026-05-14T13:30:00Z"
    assert next_run["market_closed"] is True


def test_write_closed_artifacts_handles_missing_next_open(tmp_state):
    """If next_open is None, next_run_at is empty string (not None);
    deploy/run_scheduler.sh skips empty values gracefully and the
    daily fallback timer still covers us."""
    rid = state.new_run_id()
    ms = market_gate.MarketState(
        is_open=False, next_open=None, rationale="test",
    )
    next_run = market_gate.write_closed_artifacts(rid, ms)
    assert next_run["next_run_at"] == ""


def test_write_closed_artifacts_marks_clock_error(tmp_state):
    """A fail-closed clock error routes through the same closed-market
    artifacts but carries clock_error=True (so the dashboard can flag a
    broker-connectivity problem) and an empty next_run_at (next_open is
    unknown → daily fallback timer covers the re-run)."""
    rid = state.new_run_id()
    ms = market_gate.MarketState(
        is_open=False, next_open=None,
        rationale="market_gate: broker clock fetch failed; failing closed",
        clock_error=True,
    )
    next_run = market_gate.write_closed_artifacts(rid, ms)
    assert next_run["next_run_at"] == ""
    assert next_run["clock_error"] is True

    gate = json.loads((state.run_dir(rid) / "market_gate.json").read_text())
    assert gate["clock_error"] is True
    assert gate["is_open"] is False
