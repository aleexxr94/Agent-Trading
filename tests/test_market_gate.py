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


def test_check_returns_open_when_broker_does_not_implement_clock():
    """Default Broker.get_clock returns None; fall open conservatively."""
    ms = market_gate.check(_StubBroker(clock=None))
    assert ms.is_open is True
    assert "did not return clock" in ms.rationale.lower()


def test_check_returns_open_when_broker_raises():
    """Transient API error → fall open. The order-side market-hours
    check at Alpaca still rejects bad-time orders, so a brief gate
    failure doesn't risk submitting orders into a closed market."""
    ms = market_gate.check(_StubBroker(clock_raises=RuntimeError("network glitch")))
    assert ms.is_open is True
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
