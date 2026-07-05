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


def test_memo_uses_raw_costs_ignoring_dashboard_reset(tmp_state):
    """Codex P2 regression (PR #109): the dashboard's "reset all LLM
    costs" marker is display-only. The memo is pipeline-facing
    calibration evidence — a UI reset must not make past trades look
    more profitable to the agents (same principle as the raw-cost
    sizing NAV in lib.dashboard_data)."""
    state.append_trade(_fill(
        "TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1",
    ))
    state.append_trade(_fill(
        "TQQQ", "sell", 4, 71.0, at="2026-06-03T14:00:00Z", aid="a2",
    ))
    state.COSTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    state.COSTS_LOG.write_text(json.dumps(
        {"run_id": "r1", "cost_usd": 1.0, "at": "2026-06-01T14:05:00Z"},
    ) + "\n")
    state.set_all_time_cost_reset()  # operator resets AFTER the trade closed
    memo = feedback.build_performance_memo()
    assert memo["overall"]["net_pnl_usd"] == 3.0  # +4 gross − 1.0 LLM, reset ignored


def test_memo_skips_corrupt_cost_lines(tmp_state):
    """One garbage line in costs.jsonl degrades that row, not the memo."""
    state.append_trade(_fill(
        "TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1",
    ))
    state.append_trade(_fill(
        "TQQQ", "sell", 4, 71.0, at="2026-06-03T14:00:00Z", aid="a2",
    ))
    state.COSTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    state.COSTS_LOG.write_text(
        "{not json}\n"
        + json.dumps({"run_id": "r1", "cost_usd": 1.0, "at": "2026-06-01T14:05:00Z"})
        + "\n"
    )
    memo = feedback.build_performance_memo()
    assert memo["overall"]["net_pnl_usd"] == 3.0


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


# ---------- era split (paper→live memory continuity) ----------


def test_memo_all_paper_output_has_no_era_keys(tmp_state):
    """Byte-stability: while paper-only, the memo must be identical to the
    pre-era-tagging output — it feeds the cycle-dedup fingerprint and cached
    prompts. No era_split key, no mode on recent_exits."""
    rows = [
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 80.0, at="2026-06-03T14:00:00Z", aid="a2"),
    ]
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[], kill_events=[])
    assert set(memo.keys()) == {
        "closed_trades", "overall", "by_factor", "confidence_calibration",
        "by_regime", "recent_exits",
    }
    assert all("mode" not in r for r in memo["recent_exits"])


def test_memo_era_split_from_live_tagged_fills(tmp_state):
    """Mixed history without a transition marker: live_since falls back to
    the earliest live fill; combined sections keep the FULL history."""
    rows = [
        # Paper era: TQQQ +40 win.
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 80.0, at="2026-06-03T14:00:00Z", aid="a2"),
        # Live era: SOXL -30 loss.
        {**_fill("SOXL", "buy", 10, 30.0, run_id="r2", at="2026-07-01T14:00:00Z", aid="a3"),
         "mode": "live"},
        {**_fill("SOXL", "sell", 10, 27.0, at="2026-07-02T14:00:00Z", aid="a4"),
         "mode": "live"},
    ]
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[], kill_events=[])
    # Combined sections still cover everything (the carryover the user wants).
    assert memo["closed_trades"] == 2
    assert memo["overall"]["trades"] == 2
    es = memo["era_split"]
    assert es["live_since"] == "2026-07-01T14:00:00Z"
    assert es["paper"]["trades"] == 1
    assert es["paper"]["wins"] == 1
    assert es["paper"]["through"] == "2026-06-03T14:00:00Z"
    assert es["live"]["trades"] == 1
    assert es["live"]["losses"] == 1
    modes = {r["symbol"]: r["mode"] for r in memo["recent_exits"]}
    assert modes == {"TQQQ": "paper", "SOXL": "live"}


def test_memo_era_split_activates_from_marker_and_uses_trade_modes(tmp_state):
    """The write-once marker activates the era split even before any live
    fill exists, and era assignment comes from each closed trade's own mode
    (exact — stamped by the era-split FIFO matcher), not timestamps.
    Untagged rows predate tagging and are paper by construction."""
    state.write_live_transition_once(
        live_starting_equity_usd=2600.0, nav_cap_usd=2500.0,
        run_id="r9", live_version=1,
    )
    marker_at = state.read_live_transition()["at"]
    rows = [
        # Untagged legacy fills → paper.
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2020-01-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 80.0, at="2020-01-03T14:00:00Z", aid="a2"),
        # Live-tagged fills → live, regardless of any timestamp heuristics.
        {**_fill("SOXL", "buy", 10, 30.0, run_id="r2", at="2026-07-02T14:00:00Z", aid="a3"),
         "mode": "live"},
        {**_fill("SOXL", "sell", 10, 27.0, at="2026-07-02T18:00:00Z", aid="a4"),
         "mode": "live"},
    ]
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[], kill_events=[])
    es = memo["era_split"]
    # live_since = earlier of marker vs first live fill.
    assert es["live_since"] == min(marker_at, "2026-07-02T14:00:00Z")
    assert es["paper"]["trades"] == 1
    assert es["live"]["trades"] == 1
    modes = {r["symbol"]: r["mode"] for r in memo["recent_exits"]}
    assert modes == {"TQQQ": "paper", "SOXL": "live"}


def test_memo_exit_attribution_does_not_cross_eras(tmp_state):
    """Codex P2 (PR #112): a live exit inside the 6h match window of a PAPER
    kill event on the same symbol must not inherit its exit_kind."""
    rows = [
        {**_fill("TQQQ", "buy", 4, 70.0, run_id="r2", at="2026-07-01T13:00:00Z", aid="a3"),
         "mode": "live"},
        {**_fill("TQQQ", "sell", 4, 60.0, at="2026-07-01T15:00:00Z", aid="a4"),
         "mode": "live"},
    ]
    kill_events = [
        # Paper stop-out on the same symbol 1h before the live close —
        # inside the 6h window, but from the other era.
        {"at": "2026-07-01T14:00:00Z", "symbol": "TQQQ",
         "reason": "loss cap", "exit_kind": "loss_cap", "mode": "paper"},
    ]
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[],
                                           kill_events=kill_events)
    (exit_row,) = memo["recent_exits"]
    assert exit_row["mode"] == "live"
    assert exit_row["exit_kind"] == "agent_decision"  # NOT loss_cap

    # Same setup but the event is live too → attribution applies.
    kill_events[0]["mode"] = "live"
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[],
                                           kill_events=kill_events)
    assert memo["recent_exits"][0]["exit_kind"] == "loss_cap"


def test_live_since_uses_earlier_of_marker_and_first_live_fill(tmp_state):
    """Codex P2 (PR #112): a dashboard resync can land live fills BEFORE the
    first live cycle writes the marker — those real-money trades must not be
    re-classified as paper by the later marker timestamp."""
    rows = [
        {**_fill("TQQQ", "buy", 4, 70.0, run_id="r2", at="2026-07-01T13:00:00Z", aid="a1"),
         "mode": "live"},
        {**_fill("TQQQ", "sell", 4, 60.0, at="2026-07-01T15:00:00Z", aid="a2"),
         "mode": "live"},
    ]
    # Marker written AFTER those fills (first orchestrator cycle came later).
    state.write_live_transition_once(
        live_starting_equity_usd=5000.0, nav_cap_usd=None,
        run_id="r9", live_version=1,
    )
    memo = feedback.build_performance_memo(trade_rows=rows, cost_rows=[], kill_events=[])
    es = memo["era_split"]
    assert es["live_since"] == "2026-07-01T13:00:00Z"  # first live fill, not marker
    assert es["live"]["trades"] == 1
    assert es["paper"]["trades"] == 0
    assert memo["recent_exits"][0]["mode"] == "live"


def test_memo_recent_exits_tag_manual_close(tmp_state):
    """A dashboard manual close (source=dashboard kill event within 6h of
    the closing fill) is attributed to the operator; the same event >6h
    away degrades to agent_decision — documents the after-hours gap where
    Alpaca queues the close for next open."""
    rows = [
        _fill("TQQQ", "buy", 4, 70.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a1"),
        _fill("TQQQ", "sell", 4, 80.0, at="2026-06-03T14:00:00Z", aid="a2"),
        _fill("SOXL", "buy", 10, 30.0, run_id="r1", at="2026-06-01T14:00:00Z", aid="a3"),
        _fill("SOXL", "sell", 10, 40.0, at="2026-06-04T14:00:00Z", aid="a4"),
    ]
    kill_events = [
        # 30 min before TQQQ's close → attributed to the operator.
        {"at": "2026-06-03T13:30:00Z", "symbol": "TQQQ",
         "reason": "manual close from dashboard",
         "exit_kind": "manual_close", "source": "dashboard"},
        # SOXL manual-close event 20h before the fill → outside the 6h
        # window, falls back to agent_decision.
        {"at": "2026-06-03T18:00:00Z", "symbol": "SOXL",
         "reason": "manual close from dashboard",
         "exit_kind": "manual_close", "source": "dashboard"},
    ]
    memo = feedback.build_performance_memo(
        trade_rows=rows, cost_rows=[], kill_events=kill_events,
    )
    exits = {r["symbol"]: r for r in memo["recent_exits"]}
    assert exits["TQQQ"]["exit_kind"] == "manual_close"
    assert exits["SOXL"]["exit_kind"] == "agent_decision"
