"""Tests for lib/manual_actions — the dashboard's manual position close."""
from __future__ import annotations

import json

from lib import manual_actions, state


class _FakeBroker:
    is_paper = True

    def __init__(self, flatten_fails=False, flatten_raises=False):
        self._flatten_fails = flatten_fails
        self._flatten_raises = flatten_raises
        self.flatten_log: list[str] = []

    def flatten(self, symbol):
        self.flatten_log.append(symbol)
        if self._flatten_raises:
            raise ConnectionError("broker down")
        if self._flatten_fails:
            return None  # Broker contract: None = close rejected/failed
        return {"symbol": symbol, "status": "accepted", "broker_order_id": "o1"}


def _kill_events():
    return state.read_kill_events()


def test_manual_close_success_records_kill_event(tmp_state):
    broker = _FakeBroker()
    res = manual_actions.close_position_manually(
        "TQQQ", broker=broker, sync_fills=False,
    )
    assert res.ok is True
    assert res.kill_event_written is True
    assert broker.flatten_log == ["TQQQ"]
    events = _kill_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["symbol"] == "TQQQ"
    assert ev["exit_kind"] == "manual_close"
    assert ev["source"] == "dashboard"
    assert ev["mode"] == "paper"
    assert ev["reason"] == manual_actions.MANUAL_CLOSE_REASON


def test_manual_close_rejected_records_nothing(tmp_state):
    """A failed flatten leaves the position open — a phantom kill event
    would mis-attribute a later unrelated close in the memo's 6h window."""
    state.write_position_peaks({"TQQQ": {"peak_mark": 90.0, "updated_at": "x"}})
    res = manual_actions.close_position_manually(
        "TQQQ", broker=_FakeBroker(flatten_fails=True), sync_fills=False,
    )
    assert res.ok is False
    assert res.error
    assert _kill_events() == []
    # Peak retained — the position is still open, the stop must keep firing
    # from the same high-water mark.
    assert "TQQQ" in state.read_position_peaks()


def test_manual_close_broker_unavailable(tmp_state, monkeypatch):
    import lib.alpaca_client as ac

    def _boom(*a, **kw):
        raise RuntimeError("no keys")

    monkeypatch.setattr(ac, "AlpacaBroker", _boom)
    res = manual_actions.close_position_manually("TQQQ", sync_fills=False)
    assert res.ok is False
    assert "broker unavailable" in res.error
    assert _kill_events() == []


def test_manual_close_pops_trailing_peak(tmp_state):
    """Accepted close drops the symbol's peak so a re-entry can't be
    trailing-stopped against the prior trade's ratchet (monitor parity)."""
    state.write_position_peaks({
        "TQQQ": {"peak_mark": 90.0, "updated_at": "x"},
        "SOXL": {"peak_mark": 40.0, "updated_at": "x"},
    })
    res = manual_actions.close_position_manually(
        "TQQQ", broker=_FakeBroker(), sync_fills=False,
    )
    assert res.ok is True
    peaks = state.read_position_peaks()
    assert "TQQQ" not in peaks
    assert "SOXL" in peaks  # untouched


def test_manual_close_sync_failure_is_nonfatal(tmp_state, monkeypatch):
    from lib import trades_sync

    def _boom(**kw):
        raise ConnectionError("activities endpoint down")

    monkeypatch.setattr(trades_sync, "sync_fills_from_alpaca", _boom)
    res = manual_actions.close_position_manually(
        "TQQQ", broker=_FakeBroker(), sync_fills=True,
    )
    assert res.ok is True
    assert res.sync_error is not None
    assert "ConnectionError" in res.sync_error
    assert len(_kill_events()) == 1  # kill event written regardless


def test_manual_close_allowed_while_halted(tmp_state):
    """halt.flag stops the agents, not the operator's risk reduction —
    deliberate divergence from monitor.execute_actions."""
    state.set_halt("test")
    res = manual_actions.close_position_manually(
        "TQQQ", broker=_FakeBroker(), sync_fills=False,
    )
    assert res.ok is True
    assert len(_kill_events()) == 1


def test_manual_close_feeds_memo_attribution(tmp_state):
    """End-to-end memory contract: after a manual close whose SELL fill is
    on the trade log, the performance memo attributes the exit to the
    operator (manual_close), not to an agent decision."""
    from lib import feedback

    res = manual_actions.close_position_manually(
        "TQQQ", broker=_FakeBroker(), sync_fills=False,
    )
    assert res.ok
    ev = _kill_events()[0]
    close_at = ev["at"]
    rows = [
        {"activity_id": "a1", "alpaca_order_id": "o0", "run_id": "r1",
         "symbol": "TQQQ", "kind": "etf", "side": "buy", "qty": 4,
         "fill_price": 70.0, "fees_usd": 0.0,
         "filled_at": "2026-06-01T14:00:00Z"},
        {"activity_id": "a2", "alpaca_order_id": "o1", "run_id": None,
         "symbol": "TQQQ", "kind": "etf", "side": "sell", "qty": 4,
         "fill_price": 80.0, "fees_usd": 0.0, "filled_at": close_at},
    ]
    memo = feedback.build_performance_memo(
        trade_rows=rows, cost_rows=[], kill_events=_kill_events(),
    )
    exits = {r["symbol"]: r for r in memo["recent_exits"]}
    assert exits["TQQQ"]["exit_kind"] == "manual_close"
