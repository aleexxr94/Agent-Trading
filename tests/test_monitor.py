"""Tests for monitor.py — kill-condition evaluation with mocked broker marks.

Doesn't actually call AlpacaBroker — monkeypatches _try_load_broker to
inject a fake instance so we exercise the live-marks path without keys.
"""
from __future__ import annotations

import json

import pytest

import monitor
from lib import state
from lib.broker import BrokerPosition


def _bp(symbol, qty, market_value, asset_class="us_equity") -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol, qty=qty,
        avg_cost=market_value / qty if qty else 0,
        market_value=market_value, unrealized_pl_usd=0.0,
        asset_class=asset_class,
    )


class _FakeBroker:
    def __init__(self, positions, flatten_log=None):
        self._positions = positions
        self._flatten_log = flatten_log if flatten_log is not None else []

    @property
    def name(self):
        return "fake"

    def get_account(self):
        raise NotImplementedError

    def get_positions(self):
        return self._positions

    def submit_order(self, *a, **kw):
        raise NotImplementedError

    def cancel_all(self):
        return 0

    def flatten(self, symbol):
        self._flatten_log.append(symbol)
        return None


def _write_portfolio(positions, **over):
    portfolio = {
        "run_id": "test", "generated_at": state.utcnow_iso(),
        "nav_usd": 2500.0, "cash_usd": 280.0, "cash_buffer_pct": 11.2,
        "all_cash": False, "all_cash_rationale": None,
        "construction_rationale": "x",
        "positions": positions,
    }
    portfolio.update(over)
    state.write_json(state.CURRENT_PORTFOLIO, portfolio)


def _etf_pos(**over):
    base = {
        "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
        "leverage_factor": 3.0, "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 11.2,
    }
    base.update(over)
    return base


def test_monitor_no_portfolio_no_op(tmp_state, capsys):
    rc = monitor.main([])
    assert rc == 0
    assert "No current_portfolio.json" in capsys.readouterr().out


def test_monitor_halt_flag_short_circuits(tmp_state, capsys):
    state.set_halt("test")
    rc = monitor.main([])
    assert rc == 0
    assert "halt.flag set" in capsys.readouterr().out


def test_monitor_no_broker_no_marks_no_kill(tmp_state, monkeypatch, capsys):
    """Without a broker, marks dict is empty; monitor still runs but
    can't evaluate price-based kill conditions on any position."""
    _write_portfolio([_etf_pos()])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: None)
    rc = monitor.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 marks" in out and "broker=off" in out


def test_monitor_flattens_when_loss_exceeds_25_pct(tmp_state, monkeypatch):
    """ETF entered at $70, current mark $52 = 25.7% loss → must flatten."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    flat_log: list = []
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 52.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["TQQQ"]


def test_monitor_does_not_flatten_at_24_pct_loss(tmp_state, monkeypatch):
    """24% loss is under the 25% kill — no flatten action."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    flat_log: list = []
    # 4 shares at $53.20 = $212.80 vs cost basis $280 = 24% loss
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 53.20)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == []


def test_monitor_dry_run_evaluates_but_does_not_flatten(tmp_state, monkeypatch):
    """--dry-run computes actions but never calls broker.flatten."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    flat_log: list = []
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 50.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main(["--dry-run"])
    assert flat_log == []


def test_monitor_skips_position_with_no_mark(tmp_state, monkeypatch):
    """Position present in portfolio but broker reports no holding → can't
    evaluate, must skip rather than crash."""
    _write_portfolio([
        _etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0),
        _etf_pos(symbol="SOXL", shares=10, avg_cost=25.0),
    ])
    flat_log: list = []
    # Only TQQQ in broker; SOXL is missing
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 80.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    # No kills (TQQQ is in profit, SOXL is unmark-able) — and definitely no crash
    assert flat_log == []
