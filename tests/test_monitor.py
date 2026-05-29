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


def _bp(symbol, qty, market_value, asset_class="us_equity", avg_cost=None) -> BrokerPosition:
    # avg_cost defaults to market_value/qty (i.e. no P&L) unless overridden —
    # the monitor now uses the broker's avg_entry_price as the cost basis.
    return BrokerPosition(
        symbol=symbol, qty=qty,
        avg_cost=avg_cost if avg_cost is not None else (market_value / qty if qty else 0),
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
    """ETF entered at $70 (broker avg_cost), current mark $52 = 25.7% loss → flatten."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    flat_log: list = []
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 52.0, avg_cost=70.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["TQQQ"]


def test_monitor_does_not_flatten_at_24_pct_loss(tmp_state, monkeypatch):
    """24% loss is under the 25% kill — no flatten action."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    flat_log: list = []
    # 4 shares at $53.20 = $212.80 vs broker cost basis $280 = 24% loss
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 53.20, avg_cost=70.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == []


def test_monitor_dry_run_evaluates_but_does_not_flatten(tmp_state, monkeypatch):
    """--dry-run computes actions but never calls broker.flatten."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    flat_log: list = []
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 50.0, avg_cost=70.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main(["--dry-run"])
    assert flat_log == []


def test_monitor_uses_broker_cost_basis_not_portfolio(tmp_state, monkeypatch):
    """Broker truth (Finding 5): loss is computed from the broker's
    avg_entry_price, not the portfolio's stored avg_cost. Portfolio says
    $100 (would be a 52% loss → flatten); broker says $50 (4% loss → hold)."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=100.0)])
    flat_log: list = []
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 48.0, avg_cost=50.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == []  # 4% broker loss, not the 52% the portfolio basis implies


def test_monitor_etf_flatten_on_full_loss(tmp_state):
    """An ETF position whose mark has collapsed past the 25% loss cap is
    flattened by its own symbol."""
    etf_pos = {
        "kind": "etf", "symbol": "SOXL", "shares": 10, "avg_cost": 25.0,
        "leverage_factor": 3.0, "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 10.0,
    }
    portfolio = {"nav_usd": 2500.0, "positions": [etf_pos]}
    # Mark 15 vs cost 25 = 40% loss → past the 25% cap.
    actions = monitor.evaluate_portfolio(portfolio=portfolio, marks={"SOXL": 15.0})
    assert len(actions) == 1
    assert actions[0]["action"] == "flatten"
    assert actions[0]["symbol"] == "SOXL"


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


def _read_shadow() -> list:
    if not state.MONITOR_SHADOW_LOG.exists():
        return []
    return [
        json.loads(line)
        for line in state.MONITOR_SHADOW_LOG.read_text().splitlines()
        if line.strip()
    ]


def test_monitor_enforces_time_stop(tmp_state, monkeypatch):
    """Phase 1: a past time_stop_utc flattens the position even when it's in
    profit (loss cap not hit). The audit records the fire."""
    past = "2000-01-01T00:00:00Z"
    _write_portfolio([_etf_pos(
        symbol="TQQQ", shares=4, avg_cost=80.0,
        kill_conditions={"max_loss_pct": 25, "time_stop_utc": past},
    )])
    flat_log: list = []
    # In profit (mark 80 == cost 80) so only the time stop can fire.
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 80.0, avg_cost=80.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["TQQQ"]
    fired = _read_shadow()[-1]["fired"]
    assert any(f["symbol"] == "TQQQ" and "time stop" in f["reason"] for f in fired)


def test_monitor_audit_flags_orphans_and_missing(tmp_state, monkeypatch):
    """Phase 1 audit: surface target-vs-broker drift — broker holds a symbol
    the target doesn't (orphan) and the target names one the broker doesn't."""
    _write_portfolio([_etf_pos(symbol="SOXL", shares=10, avg_cost=25.0)])
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 80.0, avg_cost=80.0)])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    cov = _read_shadow()[-1]["coverage"]
    assert cov["orphans"] == ["TQQQ"]
    assert cov["missing"] == ["SOXL"]
    assert cov["unmarked"] == 1  # SOXL has no broker mark


def test_monitor_enforces_etf_price_stop_via_mark(tmp_state):
    """Phase 1: an ETF price stop fires off the mark (spot=mark) even when the
    loss cap isn't hit. Mark 50 ≤ kill_below 60, while loss is only ~4%."""
    portfolio = {"positions": [{
        "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 52.0,
        "kill_conditions": {"max_loss_pct": 25, "underlying_price_below": 60.0},
        "position_pct": 11.2,
    }]}
    bp = _bp("TQQQ", 4, 4 * 50.0, avg_cost=52.0)
    actions = monitor.evaluate_portfolio(
        portfolio=portfolio, marks={"TQQQ": 50.0},
        cost_basis={"TQQQ": 52.0}, broker_positions=[bp],
    )
    assert any(a["symbol"] == "TQQQ" and a["action"] == "flatten" for a in actions)
    assert "kill_below" in actions[0]["reason"]


def test_monitor_flattens_orphan_on_loss_cap(tmp_state, monkeypatch):
    """Phase 1: a broker position the target portfolio doesn't name still gets
    the hard loss cap — nothing held goes unmonitored (Finding 5)."""
    _write_portfolio([])  # all-cash target, but the broker still holds SQQQ
    flat_log: list = []
    fake = _FakeBroker(
        [_bp("SQQQ", 10, 10 * 40.0, avg_cost=60.0)], flatten_log=flat_log,
    )  # cost $600, value $400 → 33% loss
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["SQQQ"]
    assert any("orphan" in f["reason"] for f in _read_shadow()[-1]["fired"])


def test_monitor_etf_loss_cap_via_broker_value_without_mark(tmp_state):
    """With no marks dict, the loss cap still fires off the broker's
    market_value. An ETF at $0 market value vs $650 cost = 100% loss →
    flatten, no price/time stop needed."""
    etf_pos = {
        "kind": "etf", "symbol": "SOXL", "shares": 10, "avg_cost": 65.0,
        "leverage_factor": 3.0, "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},  # loss cap only
        "position_pct": 10.0,
    }
    bp = BrokerPosition(
        symbol="SOXL", qty=10, avg_cost=65.0, market_value=0.0,
        unrealized_pl_usd=-650.0, asset_class="us_equity",
    )
    actions = monitor.evaluate_portfolio(
        portfolio={"positions": [etf_pos]},
        marks={},  # no mark; loss cap must use broker market_value
        broker_positions=[bp],
    )
    assert len(actions) == 1
    assert actions[0]["symbol"] == "SOXL"
    assert "cap" in actions[0]["reason"]


def test_monitor_leaves_legacy_option_orphan_alone(tmp_state):
    """A stray/legacy us_option position the ETF-only target doesn't name is
    LEFT ALONE — the system never opens, sizes, or auto-flattens options. It
    stays visible via the audit orphan list but monitor emits no action for
    it (owner decision: no auto-flatten of legacy options)."""
    bp = BrokerPosition(
        symbol="SPY260619C00530000", qty=1, avg_cost=6.50, market_value=650.0,
        unrealized_pl_usd=0.0, asset_class="us_option",
    )
    actions = monitor.evaluate_portfolio(
        portfolio={"positions": []},
        marks={},
        broker_positions=[bp],
    )
    assert actions == [], "legacy option orphan must not be auto-flattened"


def test_monitor_still_covers_equity_orphan_with_option_present(tmp_state):
    """An equity orphan still gets loss-cap coverage even when a legacy
    option orphan is also held — removing the option-flatten branch must not
    disturb equity orphan handling."""
    flat_log: list = []
    option_bp = BrokerPosition(
        symbol="SPY260619C00530000", qty=1, avg_cost=6.50, market_value=650.0,
        unrealized_pl_usd=0.0, asset_class="us_option",
    )
    # SQQQ entered at $60, now $40 → 33% loss, over the 25% cap → flatten.
    equity_bp = _bp("SQQQ", 10, 10 * 40.0, avg_cost=60.0)
    actions = monitor.evaluate_portfolio(
        portfolio={"positions": []},
        marks={},
        broker_positions=[option_bp, equity_bp],
    )
    assert any(a["symbol"] == "SQQQ" and a["action"] == "flatten" for a in actions)
    assert all(a["symbol"] != "SPY260619C00530000" for a in actions)


def test_monitor_refuses_under_half_raised_live_gate(tmp_state, monkeypatch):
    """B1: monitor can flatten/cancel, so it must refuse to run when
    LIVE_TRADING_ENABLED=true while LIVE_VERSION==0 (fail closed). It must
    never even construct a broker."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("broker must not be loaded under a half-raised live gate")

    monkeypatch.setattr(monitor, "_try_load_broker", _boom)
    assert monitor.main([]) == 2


def test_monitor_kill_switch_disables_price_time_stops(tmp_state, monkeypatch):
    """MONITOR_ENFORCE_STOPS=false reverts to loss-cap-only: a tripped time
    stop does NOT flatten an otherwise-healthy position."""
    monkeypatch.setenv("MONITOR_ENFORCE_STOPS", "false")
    past = "2000-01-01T00:00:00Z"
    _write_portfolio([_etf_pos(
        symbol="TQQQ", shares=4, avg_cost=80.0,
        kill_conditions={"max_loss_pct": 25, "time_stop_utc": past},
    )])
    flat_log: list = []
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 80.0, avg_cost=80.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == []  # stops disabled; loss cap not hit


def test_monitor_audit_dd_flags_total_wipeout(tmp_state):
    """Codex P2: a latest NAV of 0.0 is a 100% drawdown the audit proxy must
    flag, not skip via a truthiness check."""
    state.append_nav({"run_id": "r1", "at": state.utcnow_iso(), "nav_usd": 2500.0})
    state.append_nav({"run_id": "r2", "at": state.utcnow_iso(), "nav_usd": 0.0})
    report = monitor.audit_report(
        portfolio={"positions": []}, broker_positions=[], marks={},
        actions=[], enforce_stops=True,
    )
    dd = report["daily_dd_shadow"]
    assert dd["would_halt_new_orders"] is True
    assert dd["dd_pct"] == 100.0


# ---- Phase 2: 8% daily-drawdown breaker ----


def test_run_dd_breaker_trips_and_writes_halt(tmp_state):
    """≥8% intraday drawdown writes the auto-expiring dd_halt flag."""
    state.set_sod_nav_today(2500.0)
    info = monitor.run_dd_breaker(current_nav=2250.0, enabled=True)  # 10% DD
    assert info["tripped"] is True
    assert info["dd_pct"] == 10.0
    assert state.dd_halt_active() is True


def test_run_dd_breaker_no_trip_under_threshold(tmp_state):
    state.set_sod_nav_today(2500.0)
    info = monitor.run_dd_breaker(current_nav=2400.0, enabled=True)  # 4% DD
    assert info["tripped"] is False
    assert state.dd_halt_active() is False


def test_run_dd_breaker_disabled_does_not_write_flag(tmp_state):
    """Kill-switch off: DD is still computed but no halt flag is written."""
    state.set_sod_nav_today(2500.0)
    info = monitor.run_dd_breaker(current_nav=2000.0, enabled=False)  # 20% DD
    assert info["tripped"] is True
    assert state.dd_halt_active() is False


def test_run_dd_breaker_dry_run_does_not_persist(tmp_state):
    """persist=False (dry-run) computes the trip but writes no halt flag."""
    state.set_sod_nav_today(2500.0)
    info = monitor.run_dd_breaker(current_nav=2000.0, enabled=True, persist=False)
    assert info["tripped"] is True
    assert state.dd_halt_active() is False


def test_run_dd_breaker_first_obs_sets_baseline(tmp_state):
    """First observation of the day sets the baseline → DD 0, no trip."""
    info = monitor.run_dd_breaker(current_nav=2400.0, enabled=True)
    assert info["tripped"] is False
    assert state.read_sod_nav_today() == 2400.0


def test_dd_halt_auto_expires_next_utc_day(tmp_state):
    state.set_dd_halt(dd_pct=10.0, sod_nav=2500.0, current_nav=2250.0)
    assert state.dd_halt_active() is True
    stale = state.read_dd_halt()
    stale["date"] = "2000-01-01"
    state.DD_HALT_FLAG.write_text(json.dumps(stale))
    assert state.dd_halt_active() is False  # prior-day flag is expired
