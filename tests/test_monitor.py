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
    def __init__(self, positions, flatten_log=None, flatten_fails=False):
        self._positions = positions
        self._flatten_log = flatten_log if flatten_log is not None else []
        self._flatten_fails = flatten_fails

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
        if self._flatten_fails:
            return None  # Broker contract: None = close rejected/failed
        return {"symbol": symbol, "status": "accepted"}


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


# ---------- trailing stops (constructor-chosen, monitor-enforced) ----------


def test_trailing_stop_ratchets_peak_and_fires():
    """Peak ratchets up with the mark; the stop fires only when the mark
    falls trailing_stop_pct below the peak."""
    portfolio = {"positions": [_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )]}
    bp = [_bp("TQQQ", 4, 4 * 100.0, avg_cost=70.0)]

    # First observation: peak initialises at the mark — never fires.
    peaks, actions = monitor.update_trailing_stops(
        portfolio=portfolio, marks={"TQQQ": 100.0},
        broker_positions=bp, position_peaks={},
    )
    assert actions == []
    assert peaks["TQQQ"]["peak_mark"] == 100.0

    # Mark rises: peak ratchets up.
    peaks, actions = monitor.update_trailing_stops(
        portfolio=portfolio, marks={"TQQQ": 120.0},
        broker_positions=bp, position_peaks=peaks,
    )
    assert actions == []
    assert peaks["TQQQ"]["peak_mark"] == 120.0

    # Pullback of 9% from peak: inside the trail — holds, peak unchanged.
    peaks, actions = monitor.update_trailing_stops(
        portfolio=portfolio, marks={"TQQQ": 109.5},
        broker_positions=bp, position_peaks=peaks,
    )
    assert actions == []
    assert peaks["TQQQ"]["peak_mark"] == 120.0

    # Pullback to -10% from peak: fires. The peak is RETAINED by the pure
    # function — main() drops it only after the broker ACCEPTS the
    # flatten, so a rejected close re-fires from the same high-water mark
    # next pass instead of re-seeding lower (Codex P2 rounds, PR #109).
    peaks, actions = monitor.update_trailing_stops(
        portfolio=portfolio, marks={"TQQQ": 108.0},
        broker_positions=bp, position_peaks=peaks,
    )
    assert len(actions) == 1
    assert actions[0]["symbol"] == "TQQQ"
    assert "trailing stop" in actions[0]["reason"]
    assert peaks["TQQQ"]["peak_mark"] == 120.0


def test_trailing_stop_ignored_when_not_configured():
    """Positions without trailing_stop_pct never enter the peak map."""
    portfolio = {"positions": [_etf_pos()]}  # no trailing_stop_pct
    peaks, actions = monitor.update_trailing_stops(
        portfolio=portfolio, marks={"TQQQ": 50.0},
        broker_positions=[_bp("TQQQ", 4, 200.0)], position_peaks={},
    )
    assert peaks == {} and actions == []


def test_trailing_stop_peak_dropped_when_position_closed():
    """A symbol no longer held drops out of the peak map so a re-entry
    starts a fresh ratchet."""
    portfolio = {"positions": [_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )]}
    peaks, _ = monitor.update_trailing_stops(
        portfolio=portfolio, marks={"TQQQ": 100.0},
        broker_positions=[],  # not held at broker
        position_peaks={"TQQQ": {"peak_mark": 150.0, "updated_at": "x"}},
    )
    assert peaks == {}


def test_trailing_stop_keeps_peak_on_transient_unmarked_cycle():
    """A cycle with no mark must not lose the ratchet."""
    portfolio = {"positions": [_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )]}
    prior = {"TQQQ": {"peak_mark": 150.0, "updated_at": "x"}}
    peaks, actions = monitor.update_trailing_stops(
        portfolio=portfolio, marks={},
        broker_positions=[_bp("TQQQ", 4, 400.0)], position_peaks=prior,
    )
    assert peaks == prior and actions == []


def test_monitor_main_enforces_trailing_stop_end_to_end(tmp_state, monkeypatch):
    """Full main() pass: configured trailing stop + persisted peak +
    broker mark below the trail → flatten + kill event logged."""
    _write_portfolio([_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )])
    state.write_position_peaks({"TQQQ": {"peak_mark": 100.0, "updated_at": "x"}})
    flat_log: list = []
    # Mark 85 = 15% below the persisted 100 peak; loss vs avg_cost 70 is a
    # GAIN, so only the trailing stop can be the trigger.
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 85.0, avg_cost=70.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["TQQQ"]
    events = state.read_kill_events()
    assert len(events) == 1
    assert events[0]["symbol"] == "TQQQ"
    assert events[0]["exit_kind"] == "trailing_stop"
    # The fired symbol's peak must not survive in the persisted file.
    assert "TQQQ" not in state.read_position_peaks()


def test_non_trailing_flatten_clears_persisted_peak(tmp_state, monkeypatch):
    """Codex P2 regression (PR #109): a flatten fired by ANY rule (here the
    25% loss cap) must drop the symbol's trailing-stop peak before the peak
    file is persisted — otherwise a re-entry could be stopped out against
    the prior trade's high-water mark."""
    _write_portfolio([_etf_pos(
        symbol="TQQQ", shares=4, avg_cost=70.0,
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )])
    state.write_position_peaks({"TQQQ": {"peak_mark": 100.0, "updated_at": "x"}})
    flat_log: list = []
    # Mark 52 vs avg_cost 70 = ~26% loss → the loss cap fires (the trailing
    # threshold 90 also breached, but loss-cap evaluation runs first).
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 52.0, avg_cost=70.0)], flatten_log=flat_log)
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["TQQQ"]
    assert "TQQQ" not in state.read_position_peaks()


def test_failed_flatten_keeps_peak_and_records_no_kill_event(tmp_state, monkeypatch):
    """Codex P2 regression (PR #109): a REJECTED/FAILED close (flatten →
    None) must not record a kill event (the position is still open; a
    phantom event could mis-attribute a later unrelated close in the
    memo's match window) and must keep the trailing-stop peak so the stop
    re-fires from the same high-water mark next pass."""
    _write_portfolio([_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )])
    state.write_position_peaks({"TQQQ": {"peak_mark": 100.0, "updated_at": "x"}})
    flat_log: list = []
    # Mark 85 = 15% below the 100 peak → trailing stop fires; gain vs
    # avg_cost 70, so the loss cap stays quiet.
    fake = _FakeBroker(
        [_bp("TQQQ", 4, 4 * 85.0, avg_cost=70.0)],
        flatten_log=flat_log, flatten_fails=True,
    )
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert flat_log == ["TQQQ"]  # close was attempted...
    assert state.read_kill_events() == []  # ...but not recorded as an exit
    assert state.read_position_peaks()["TQQQ"]["peak_mark"] == 100.0


def test_positions_fetch_failure_leaves_peaks_untouched(tmp_state, monkeypatch):
    """Codex P2 regression (PR #109): a broker outage (get_positions
    raises) yields an empty positions list indistinguishable from a
    closed-out account — the peak file must be left as-is, not wiped."""
    _write_portfolio([_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )])
    prior = {"TQQQ": {"peak_mark": 150.0, "updated_at": "x"}}
    state.write_position_peaks(prior)

    class _OutageBroker(_FakeBroker):
        def get_positions(self):
            raise ConnectionError("broker unreachable")

    monkeypatch.setattr(monitor, "_try_load_broker", lambda: _OutageBroker([]))
    monitor.main([])
    assert state.read_position_peaks() == prior


def test_execute_actions_returns_accepted_flattens_only(tmp_state):
    """execute_actions reports the symbols whose close the broker
    accepted; rejected closes are excluded and unlogged."""
    actions = [
        {"symbol": "TQQQ", "action": "flatten", "reason": "loss 26% ≥ 25% cap"},
        {"symbol": "TMF", "action": "flatten", "reason": "loss 27% ≥ 25% cap"},
    ]
    ok = _FakeBroker([])
    assert monitor.execute_actions(actions, broker=ok) == {"TQQQ", "TMF"}
    assert len(state.read_kill_events()) == 2

    state.KILL_EVENTS_LOG.unlink()
    bad = _FakeBroker([], flatten_fails=True)
    assert monitor.execute_actions(actions, broker=bad) == set()
    assert state.read_kill_events() == []


def test_monitor_flatten_appends_kill_event(tmp_state, monkeypatch):
    """A loss-cap flatten records an exit-outcome row with exit_kind."""
    _write_portfolio([_etf_pos(symbol="TQQQ", shares=4, avg_cost=70.0)])
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 52.0, avg_cost=70.0)])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    events = state.read_kill_events()
    assert len(events) == 1
    assert events[0]["symbol"] == "TQQQ"
    assert events[0]["exit_kind"] == "loss_cap"
    assert events[0]["source"] == "monitor"


def test_monitor_dry_run_writes_no_kill_events_or_peaks(tmp_state, monkeypatch):
    _write_portfolio([_etf_pos(
        kill_conditions={"max_loss_pct": 25, "trailing_stop_pct": 10},
    )])
    fake = _FakeBroker([_bp("TQQQ", 4, 4 * 52.0, avg_cost=70.0)])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main(["--dry-run"])
    assert state.read_kill_events() == []
    assert state.read_position_peaks() == {}


def test_exit_kind_from_reason_mapping():
    assert monitor._exit_kind_from_reason("loss 26.0% ≥ 25% cap") == "loss_cap"
    assert monitor._exit_kind_from_reason("spot 40 ≤ kill_below 42") == "price_stop"
    assert monitor._exit_kind_from_reason("spot 80 ≥ kill_above 75") == "price_stop"
    assert monitor._exit_kind_from_reason("time stop 2026-01-01T00:00:00Z reached") == "time_stop"
    assert monitor._exit_kind_from_reason("trailing stop: mark 90 ≤ 91.8 (peak 102 − 10%)") == "trailing_stop"
    assert monitor._exit_kind_from_reason("orphan (not in target portfolio): loss 30% ≥ 25% cap") == "orphan_loss_cap"

def test_monitor_kill_event_carries_mode(tmp_state):
    """Kill events are era-tagged; a paper broker (no is_paper attr → env
    default with the lock down) records mode=paper."""
    actions = [{"symbol": "TQQQ", "action": "flatten", "reason": "loss 26% ≥ 25% cap"}]
    monitor.execute_actions(actions, broker=_FakeBroker([]))
    events = state.read_kill_events()
    assert events[0]["mode"] == "paper"

def test_dd_breaker_uses_real_equity_on_live_broker(tmp_state, monkeypatch):
    """On a live broker the 8% breaker denominates in real account equity,
    not paper-era synthetic units; the SOD baseline is set from it."""
    from lib.broker import Account

    class _LiveBroker(_FakeBroker):
        is_paper = False

        def get_account(self):
            return Account(cash_usd=5000.0, equity_usd=5000.0,
                           buying_power_usd=5000.0, is_paper=False)

    _write_portfolio([])
    fake = _LiveBroker([])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert state.read_sod_nav_today(mode="live") == 5000.0
    # The live baseline must NOT read as a paper baseline.
    assert state.read_sod_nav_today(mode="paper") is None


def test_dd_breaker_skips_update_when_live_equity_read_fails(tmp_state, monkeypatch):
    """Fail closed: a live broker whose equity read errors must skip the dd
    update for the pass (no baseline written from synthetic units)."""

    class _LiveBrokenBroker(_FakeBroker):
        is_paper = False
        # get_account inherits NotImplementedError from _FakeBroker

    _write_portfolio([])
    fake = _LiveBrokenBroker([])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert state.read_sod_nav_today() is None

def test_dd_breaker_rebaselines_on_same_day_mode_switch(tmp_state):
    """Codex P2 (PR #112): a paper SOD baseline written earlier the same UTC
    day must not be reused as the live baseline — promotion re-baselines
    from the first live observation."""
    state.set_sod_nav_today(2500.0, mode="paper")
    info = monitor.run_dd_breaker(current_nav=5000.0, enabled=True, mode="live")
    # Re-baselined at the live equity, not compared against the paper 2500
    # (which would read as +100% and mask any live drawdown all day).
    assert info["sod_nav_usd"] == 5000.0
    assert info["tripped"] is False
    assert state.read_sod_nav_today(mode="live") == 5000.0


def test_dd_breaker_legacy_untagged_baseline_reads_as_paper(tmp_state):
    """A pre-tagging sod_nav.json (no mode key) keeps working for paper."""
    import json as _json
    state.SOD_NAV_FILE.write_text(_json.dumps({
        "date": state.utcnow().date().isoformat(),
        "sod_nav_usd": 2500.0, "set_at": state.utcnow_iso(),
    }), encoding="utf-8")
    assert state.read_sod_nav_today(mode="paper") == 2500.0
    assert state.read_sod_nav_today(mode="live") is None

def test_dd_breaker_uses_capped_allocation_scale(tmp_state, monkeypatch):
    """Codex P1 (PR #112): with LIVE_NAV_CAP_USD set, the breaker must
    denominate in the same capped allocation the orchestrator sizes
    against, not full equity (idle cash would dilute a real drawdown)."""
    from lib.broker import Account

    class _LiveBroker(_FakeBroker):
        is_paper = False

        def get_account(self):
            return Account(cash_usd=5000.0, equity_usd=5000.0,
                           buying_power_usd=5000.0, is_paper=False)

    monkeypatch.setenv("LIVE_NAV_CAP_USD", "2500")
    _write_portfolio([])
    fake = _LiveBroker([])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: fake)
    monitor.main([])
    assert state.read_sod_nav_today(mode="live") == 2500.0  # allocation, not 5000


def test_dd_breaker_env_live_without_broker_writes_no_baseline(tmp_state, monkeypatch):
    """Codex P1 (PR #112): env lock fully raised but broker construction
    failed — the monitor must NOT fall back to the paper synthetic NAV and
    write it as a live-mode SOD baseline."""
    from lib import live_gate
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr(live_gate, "LIVE_VERSION", 1)
    _write_portfolio([])
    monkeypatch.setattr(monitor, "_try_load_broker", lambda: None)
    monitor.main([])
    assert state.read_sod_nav_today(mode="live") is None
    assert state.read_sod_nav_today(mode="paper") is None
