"""Tests for lib/feedback — the performance memo the LLM stages read."""
from __future__ import annotations

import json

from lib import feedback, state


def _fill(symbol, side, qty, price, *, run_id=None, at, aid):
    return {
        "activity_id": aid, "alpaca_order_id": f"o_{aid}", "run_id": run_id,
        "symbol": symbol, "kind": "etf", "side": side, "qty": qty,
        "fill_price": price, "fees_usd": 0.0, "filled_at": at,
    }


def _write_view(run_id: str, candidates: list[dict]) -> None:
    d = state.RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "view.json").write_text(json.dumps({
        "regime": "risk_on", "regime_rationale": "x", "candidates": candidates,
    }))


def test_memo_empty_when_no_trades(tmp_state):
    memo = feedback.build_performance_memo(trade_rows=[], cost_rows=[], kill_events=[])
    assert memo["closed_trades"] == 0
    assert "note" in memo


def test_memo_factor_record_and_overall(tmp_state):
    rows = [
        # TQQQ (nasdaq): +40 win
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 80.0, at="2026-06-03T14:00:00Z", aid="a2"),
        # SOXL (semis): -30 loss
        _fill("SOXL", "buy", 10, 30.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a3"),
        _fill("SOXL", "sell", 10, 27.0, at="2026-06-02T14:00:00Z", aid="a4"),
    ]
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[], kill_events=[])
    assert memo["closed_trades"] == 2
    assert memo["overall"]["wins"] == 1
    assert memo["overall"]["losses"] == 1
    assert memo["overall"]["net_pnl_usd"] == 10.0  # +40 - 30
    by_factor = {r["factor"]: r for r in memo["by_factor"]}
    assert by_factor["nasdaq"]["win_rate_pct"] == 100.0
    assert by_factor["semis"]["win_rate_pct"] == 0.0
    assert memo["overall"]["avg_hold_hours"] == 36.0  # (48 + 24) / 2


def test_memo_confidence_calibration_joins_view_json(tmp_state):
    _write_view("r1", [
        {"symbol": "TQQQ", "instrument_kind": "etf", "thesis": "t", "confidence": 0.8},
    ])
    rows = [
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 80.0, at="2026-06-03T14:00:00Z", aid="a2"),
        # No view for this one → "unknown" bucket.
        _fill("SOXL", "buy", 10, 30.0, run_id="r9", at="2026-06-01T14:00:00Z", aid="a3"),
        _fill("SOXL", "sell", 10, 27.0, at="2026-06-02T14:00:00Z", aid="a4"),
    ]
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[], kill_events=[])
    cal = {r["bucket"]: r for r in memo["confidence_calibration"]}
    assert cal["0.70-0.84"]["trades"] == 1
    assert cal["0.70-0.84"]["wins"] == 1
    assert cal["unknown"]["trades"] == 1


def test_memo_recent_exits_tag_kill_events(tmp_state):
    rows = [
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 50.0, at="2026-06-03T14:00:00Z", aid="a2"),
        _fill("SOXL", "buy", 10, 30.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a3"),
        _fill("SOXL", "sell", 10, 40.0, at="2026-06-04T14:00:00Z", aid="a4"),
    ]
    kill_events = [
        # Within 6h of TQQQ's close → attributed.
        {"at": "2026-06-03T13:30:00Z", "symbol": "TQQQ",
         "reason": "loss 28.6% ≥ 25% cap", "exit_kind": "loss_cap"},
        # SOXL event days away → NOT attributed.
        {"at": "2026-06-01T00:00:00Z", "symbol": "SOXL",
         "reason": "x", "exit_kind": "price_stop"},
    ]
    memo = feedback.build_performance_memo(
        trade_rows=rows, cost_rows=[], kill_events=kill_events,
    )
    exits = {r["symbol"]: r for r in memo["recent_exits"]}
    assert exits["TQQQ"]["exit_kind"] == "loss_cap"
    assert exits["SOXL"]["exit_kind"] == "agent_decision"
    # Newest exit first — the tape reads backwards.
    assert memo["recent_exits"][0]["symbol"] == "SOXL"


def test_memo_attributes_llm_cost_via_equal_split(tmp_state):
    """Net PnL in the memo is the same net the dashboard shows (gross −
    fees − attributed LLM cost)."""
    rows = [
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 71.0, at="2026-06-03T14:00:00Z", aid="a2"),
    ]
    costs = [{"run_id": "r1", "cost_usd": 1.0}]
    memo = feedback.build_performance_memo(
        trade_rows=rows, cost_rows=costs, kill_events=[],
    )
    assert memo["overall"]["net_pnl_usd"] == 3.0  # +4 gross − 1.0 LLM


def test_memo_reads_live_state_by_default(tmp_state):
    state.append_trade(_fill(
        "TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1",
    ))
    state.append_trade(_fill(
        "TQQQ", "sell", 4, 80.0, at="2026-06-02T14:00:00Z", aid="a2",
    ))
    memo = feedback.build_performance_memo()
    assert memo["closed_trades"] == 1


def test_memo_safe_wrapper_never_raises(tmp_state, monkeypatch):
    monkeypatch.setattr(
        feedback, "build_performance_memo",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert feedback.build_performance_memo_safe() is None


def test_entry_confidence_handles_missing_and_corrupt_artifacts(tmp_state):
    assert feedback.entry_confidence_for(None, "TQQQ") is None
    assert feedback.entry_confidence_for("no_such_run", "TQQQ") is None
    d = state.RUNS_DIR / "bad_run"
    d.mkdir(parents=True)
    (d / "view.json").write_text("{not json")
    assert feedback.entry_confidence_for("bad_run", "TQQQ") is None
