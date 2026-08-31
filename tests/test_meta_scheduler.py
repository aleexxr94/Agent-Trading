"""Tests for orchestrator._compute_next_run_at — the meta-scheduler caller.

Covers the four-bucket decision tree the helper implements:
  1. ctx.dry_run=True  → heuristic, no LLM call
  2. LLM raises        → heuristic + reason
  3. LLM returns junk  → heuristic + reason
  4. LLM returns valid → use it (only if within bounds)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator
from lib import state


def _portfolio(all_cash=False, positions=8):
    """Schema-valid portfolio for the meta scheduler path. PnL helper reads
    'shares' and 'avg_cost' so the NAV append doesn't blow up."""
    return {
        "run_id": "test", "nav_usd": 2500.0, "cash_usd": 280.0,
        "cash_buffer_pct": 11.2, "all_cash": all_cash,
        "positions": [
            {"kind": "etf", "symbol": f"E{i}", "shares": 4, "avg_cost": 50.0,
             "leverage_factor": 3.0, "entry_thesis": "x",
             "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0}
            for i in range(positions)
        ] if not all_cash else [],
        "construction_rationale": "x",
    }


def _ctx(dry_run=False):
    return orchestrator.StageContext(run_id="test-run", dry_run=dry_run, broker=None)


def _fake_call_factory(payload_or_exc):
    """Build a fake llm.structured_call that returns canned text or raises."""
    from lib.llm import CallUsage, StructuredCallResult

    def fake(call, **kw):
        if isinstance(payload_or_exc, Exception):
            raise payload_or_exc
        raw = payload_or_exc if isinstance(payload_or_exc, str) else json.dumps(payload_or_exc)
        return StructuredCallResult(
            payload=None, usage=CallUsage(0, 0, 0, 0),
            cost_usd=0.0, cache_hit_pct=0.0, raw_text=raw,
        )
    return fake


def test_dry_run_skips_llm_uses_heuristic(tmp_state, monkeypatch):
    """dry_run never burns an LLM call."""
    called = []
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        lambda *a, **kw: called.append(1) or None,
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(dry_run=True), portfolio=_portfolio(all_cash=False),
        view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert called == []
    assert "heuristic" in why
    # 4 hours for positions-held, matches _default_next_run_at
    parsed = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert 3.5 < (parsed - state.utcnow()).total_seconds() / 3600 < 4.5


def test_meta_returns_valid_timestamp_uses_it(tmp_state, monkeypatch):
    """LLM returns a timestamp 2h from now — orchestrator uses it verbatim."""
    target = state.utcnow() + timedelta(hours=2)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "volatile day, tighten", "hours_from_now": 2.0}),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert at == iso
    assert "orchestrator-meta" in why and "volatile day" in why


def test_meta_llm_exception_falls_back_to_heuristic(tmp_state, monkeypatch):
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory(RuntimeError("simulated 500")),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(all_cash=True), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert "meta call failed" in why
    # All-cash heuristic = 6h
    parsed = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert 5.5 < (parsed - state.utcnow()).total_seconds() / 3600 < 6.5


def test_meta_malformed_json_falls_back(tmp_state, monkeypatch):
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory("this is not JSON"),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert "meta call failed" in why  # JSON parse error caught in same except


def test_meta_malformed_timestamp_falls_back(tmp_state, monkeypatch):
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": "yesterday", "rationale": "lol"}),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert "malformed" in why


@pytest.mark.parametrize("hours_away", [0.5, 25, 48, 168])
def test_meta_out_of_bounds_falls_back(tmp_state, monkeypatch, hours_away):
    """Below 1h or above 24h must trigger the heuristic fallback."""
    target = state.utcnow() + timedelta(hours=hours_away)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "x"}),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert "out-of-bounds" in why


def test_meta_at_exactly_min_boundary_accepted(tmp_state, monkeypatch):
    """Regression: Codex review on PR #19 flagged that the model picking
    its documented minimum of 1h, in second-precision ISO format, was
    systematically rejected by the bounds check because state.utcnow()
    is microsecond-precision. Verify the tolerance fix accepts it.

    Reproduces the failure mode by snapping the target to whole-second
    ISO format AFTER capturing a microsecond `now` from inside the
    helper's clock source.
    """
    # state.utcnow() returns microseconds; the model's `next_run_at` is
    # second-precision, so even an honest "now + exactly 1h" pick lands
    # ~0.5s short of 3600s when measured against the captured `now`.
    now_mu = state.utcnow()
    target = now_mu + timedelta(hours=1)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")  # drops the fractional second
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "tight cadence ok"}),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert at == iso, f"1h boundary should be accepted, got fallback: {why}"
    assert "orchestrator-meta" in why


def test_meta_at_exactly_max_boundary_accepted(tmp_state, monkeypatch):
    """Symmetric to above — the 24h documented max should also pass even
    with second-precision truncation."""
    target = state.utcnow() + timedelta(hours=24)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "max cadence ok"}),
    )
    at, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert at == iso
    assert "orchestrator-meta" in why


def test_meta_just_below_min_boundary_still_rejected(tmp_state, monkeypatch):
    """The tolerance (30s) doesn't open the floodgates — something
    materially below 1h still falls back."""
    target = state.utcnow() + timedelta(hours=1) - timedelta(seconds=120)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "too tight"}),
    )
    _, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert "out-of-bounds" in why


def test_meta_rationale_truncated_to_300_chars(tmp_state, monkeypatch):
    """Defence vs an LLM that returns a wall of text in 'rationale'."""
    target = state.utcnow() + timedelta(hours=3)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "X" * 1000}),
    )
    _, why, _ = orchestrator._compute_next_run_at(
        ctx=_ctx(), portfolio=_portfolio(), view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    # 'orchestrator-meta: ' prefix + 300 chars of rationale
    assert len(why) < 350


def test_stage_execute_uses_meta_decision(tmp_state, monkeypatch):
    """End-to-end: stage_execute pipes scenarios_out through to the meta
    helper, and the resulting next_run.json carries the meta rationale."""
    target = state.utcnow() + timedelta(hours=2)
    iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({"next_run_at": iso, "rationale": "meta said so"}),
    )
    out = orchestrator.stage_execute(
        _ctx(dry_run=False),
        _portfolio(),
        view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert out["next_run_at"] == iso
    assert "orchestrator-meta" in out["rationale"]


# ---------- review staleness downgrade (weekend/holiday guard) ----------


def _closed_market_broker(next_open_iso: str):
    """Broker stub reporting a closed market with a known next open."""
    from lib.broker import MarketClock

    class _B:
        def get_clock(self):
            return MarketClock(
                is_open=False, next_open=next_open_iso,
                next_close="", timestamp="2026-08-28T21:00:00Z",
            )
    return _B()


def _review_pick(monkeypatch, hours_from_now: float) -> str:
    iso = (state.utcnow() + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        orchestrator.llm, "structured_call",
        _fake_call_factory({
            "next_run_at": iso, "rationale": "post-close reflection",
            "cycle_intent": "review",
        }),
    )
    return iso


def test_weekend_review_pick_downgraded_to_trade(tmp_state, monkeypatch):
    """A review scheduled ~54h before the next open (Friday night → Monday
    bell) is stale reflection — downgrade so the gate $0-skips it."""
    _review_pick(monkeypatch, hours_from_now=6)
    next_open = (state.utcnow() + timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = orchestrator.StageContext(
        run_id="test-run", dry_run=False,
        broker=_closed_market_broker(next_open),
    )
    at, why, intent = orchestrator._compute_next_run_at(
        ctx=ctx, portfolio=_portfolio(),
        view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert intent == "trade"
    assert "downgraded to trade" in why


def test_evening_review_pick_kept_when_open_is_near(tmp_state, monkeypatch):
    """A weekday-evening review ~10h before the next open stays a review."""
    _review_pick(monkeypatch, hours_from_now=6)
    next_open = (state.utcnow() + timedelta(hours=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = orchestrator.StageContext(
        run_id="test-run", dry_run=False,
        broker=_closed_market_broker(next_open),
    )
    _, why, intent = orchestrator._compute_next_run_at(
        ctx=ctx, portfolio=_portfolio(),
        view={"candidates": [], "regime": "neutral", "regime_rationale": "x"},
    )
    assert intent == "review"
    assert "downgraded" not in why
