"""End-to-end v2 dry-run test — acceptance criterion #2.

Runs the orchestrator pipeline against canned fixtures, asserts:
  - all v2 stage artifacts written under state/runs/{run_id}/
  - decision log has one entry per LLM-bearing stage with status=ok
  - no orders submitted (broker=None in dry-run)
  - schemas validate every artifact that has one
  - halt-flag stops the run before any artifact is written

v2 stages (dry-run path):
  signals → strategist → construct → sanity → execute

The market_gate stage doesn't run in dry-run (broker is None — the
gate's "no broker → fall open" branch).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import orchestrator
from lib import state


# Expected artifact set written by a v2 dry-run cycle. market_gate.json
# only appears on live runs (when broker is non-None), so dry-run skips it.
V2_DRY_RUN_ARTIFACTS = {
    "signals.json",
    "view.json",
    "portfolio.json",
    "critique.json",
    "sanity.json",
    "next_run.json",
}


def test_dry_run_writes_all_artifacts(tmp_state):
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid

    written = {p.name for p in rdir.iterdir() if p.is_file()}
    assert written == V2_DRY_RUN_ARTIFACTS, (
        f"v2 dry-run wrote {written}, expected {V2_DRY_RUN_ARTIFACTS}"
    )

    # Schemas were validated on write — re-validate to be explicit.
    state.validate(json.loads((rdir / "signals.json").read_text()), "signals.schema.json")
    state.validate(json.loads((rdir / "view.json").read_text()), "view.schema.json")
    state.validate(json.loads((rdir / "portfolio.json").read_text()), "portfolio.schema.json")
    state.validate(json.loads((rdir / "sanity.json").read_text()), "sanity.schema.json")


def test_dry_run_emits_decision_log(tmp_state):
    orchestrator.run_pipeline(dry_run=True)
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    # v2 dry-run logs 5 decisions: signals + strategist + construct +
    # critic + execute (market_gate doesn't fire in dry-run; sanity
    # doesn't go through _run_stage; chain lookup was removed with options).
    assert len(lines) == 5
    stages = [json.loads(line)["stage"] for line in lines]
    assert stages == [
        "signals", "strategist",
        "construct", "critic", "execute",
    ]
    for line in lines:
        row = json.loads(line)
        assert row["status"] == "ok"
        assert row["risk_warning"]


def test_dry_run_does_not_write_current_portfolio(tmp_state):
    orchestrator.run_pipeline(dry_run=True)
    assert not state.CURRENT_PORTFOLIO.exists()


def test_dry_run_writes_sanity_json_with_known_status(tmp_state):
    """Sanity report is written next to portfolio.json on every cycle.
    Fixture portfolio is constructed to pass all rules, so the dry-run
    sanity.json should have status ∈ {pass, warn} (never fail).
    """
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid
    sanity_doc = json.loads((rdir / "sanity.json").read_text())
    assert sanity_doc["run_id"] == rid
    assert sanity_doc["status"] in ("pass", "warn"), (
        f"v2 fixture portfolio expected to pass all sanity rules, got "
        f"status={sanity_doc['status']!r}; offenders: {sanity_doc['rules']}"
    )
    state.validate(sanity_doc, "sanity.schema.json")
    # Rule list mirrors lib/sanity.RULES — pin the count. The two
    # option-specific rules (straddle_requires_low_iv, option_premium_above_floor)
    # were removed with options; the ETF safety hardening added
    # symbol_in_universe, bringing the total to 10.
    assert len(sanity_doc["rules"]) == 10


def test_sanity_pass_path_writes_summary_into_next_run(tmp_state, monkeypatch):
    """Default path (SANITY_BLOCK_ON_FAIL unset): stage_execute proceeds
    normally; next_run.json carries a `sanity` field with the rollup.
    """
    monkeypatch.delenv("SANITY_BLOCK_ON_FAIL", raising=False)
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid
    next_run = json.loads((rdir / "next_run.json").read_text())
    assert "sanity" in next_run
    assert next_run["sanity"]["status"] in ("pass", "warn", "fail")
    assert set(next_run["sanity"]["summary"].keys()) == {"pass", "warn", "fail", "skip"}
    assert "sanity_block" not in next_run


def test_halt_flag_blocks_pipeline_start(tmp_state):
    state.set_halt("test")
    with pytest.raises(Exception):
        orchestrator.run_pipeline(dry_run=True)
    # No run dir should have been created
    assert not any(state.RUNS_DIR.iterdir())


def test_position_band_and_total_pct(tmp_state):
    result = orchestrator.run_pipeline(dry_run=True)
    p = result["portfolio"]
    assert 1 <= len(p["positions"]) <= 12
    assert sum(pos["position_pct"] for pos in p["positions"]) <= 100.0


def test_next_run_at_is_strictly_future(tmp_state):
    """The default cadence heuristic produces an ISO timestamp ahead of
    now. Stops a regression where a buggy formatter ever emitted past
    timestamps (the scheduler would refire instantly)."""
    result = orchestrator.run_pipeline(dry_run=True)
    nr = result["next_run"]
    at = datetime.strptime(nr["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert at > datetime.now(timezone.utc)


def test_run_pipeline_returns_v2_keys(tmp_state):
    """v2 run_pipeline result has a fixed shape: run_id + signals +
    view + portfolio + sanity + next_run. v1 had screen/research/
    chains/scenarios instead."""
    result = orchestrator.run_pipeline(dry_run=True)
    assert set(result.keys()) >= {"run_id", "signals", "view", "portfolio", "sanity", "next_run"}
    # Negative invariant: v1 keys must not appear.
    assert "screen" not in result
    assert "research" not in result
    assert "chains" not in result
    assert "scenarios" not in result


# ----- Live-mode mock test -----


def test_live_mode_writes_current_portfolio_with_mocked_llm(tmp_state, monkeypatch):
    """Live path (dry_run=False) calls the LLM. Mock structured_call to
    return canned fixture payloads + stub the broker so we don't open a
    real network connection.

    Also: stub market_gate.check to return is_open=True so the gate
    doesn't short-circuit; stub signals.compute_signals so we don't hit
    yfinance.
    """
    fixtures = {
        "signals": json.loads((Path(__file__).parent / "fixtures" / "signals.json").read_text()),
        "view": json.loads((Path(__file__).parent / "fixtures" / "view.json").read_text()),
        "portfolio": json.loads((Path(__file__).parent / "fixtures" / "portfolio.json").read_text()),
    }

    def fake_call(call, **kwargs):
        from lib.llm import CallUsage, StructuredCallResult
        if call.stage == "strategist":
            payload = fixtures["view"]
        elif call.stage == "construct":
            payload = fixtures["portfolio"]
        elif call.stage == "critic":
            payload = {
                "accept": True,
                "critique": "mock critic auto-accepts",
                "suggested_changes": [],
            }
        else:
            payload = {}
        return StructuredCallResult(
            payload=payload,
            usage=CallUsage(0, 0, 0, 0),
            cost_usd=0.0,
            cache_hit_pct=0.0,
            raw_text=json.dumps(payload),
        )

    monkeypatch.setattr(orchestrator.llm, "structured_call", fake_call)
    # Stub signals so we don't hit yfinance.
    monkeypatch.setattr(
        orchestrator.signals, "compute_signals",
        lambda *, run_id, symbols=None: fixtures["signals"],
    )
    # Stub the market gate so the live path actually enters the LLM stages.
    from lib import market_gate as mg
    monkeypatch.setattr(
        orchestrator.market_gate, "check",
        lambda broker: mg.MarketState(is_open=True, next_open=None, rationale="test: forced open"),
    )

    orchestrator.run_pipeline(dry_run=False, broker=None)
    assert state.CURRENT_PORTFOLIO.exists()
    p = json.loads(state.CURRENT_PORTFOLIO.read_text())
    assert (1 <= len(p["positions"]) <= 12) or p["all_cash"]


def test_market_gate_closed_short_circuits_pipeline(tmp_state, monkeypatch):
    """When the broker reports markets are closed, the pipeline writes
    market_gate.json + next_run.json and exits before any LLM call.
    """
    from lib import market_gate as mg
    monkeypatch.setattr(
        orchestrator.market_gate, "check",
        lambda broker: mg.MarketState(
            is_open=False,
            next_open="2026-05-13T13:30:00Z",
            rationale="test: forced closed",
        ),
    )
    # If any LLM call were attempted we'd error here — confirms the
    # short-circuit happened before stage_strategist.
    def _boom(*a, **kw):
        raise AssertionError("LLM should not be called when market is closed")
    monkeypatch.setattr(orchestrator.llm, "structured_call", _boom)

    # Need a non-None broker so the pipeline takes the live-path branch
    # that calls market_gate.check; the broker is otherwise unused.
    class _StubBroker:
        pass

    result = orchestrator.run_pipeline(dry_run=False, broker=_StubBroker())
    assert result["market_gate"]["is_open"] is False
    assert result["market_gate"]["next_open"] == "2026-05-13T13:30:00Z"

    # market_gate.json written; no signals/view/portfolio/sanity.
    rdir = state.RUNS_DIR / result["run_id"]
    files = {p.name for p in rdir.iterdir() if p.is_file()}
    assert "market_gate.json" in files
    assert "signals.json" not in files
    assert "view.json" not in files
    assert "portfolio.json" not in files

    # Decision log got one row with skipped_market_closed status.
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["stage"] == "market_gate"
    assert row["status"] == "skipped_market_closed"


def test_clock_error_short_circuits_pipeline_with_distinct_status(tmp_state, monkeypatch):
    """A1: a present broker whose clock is unreachable fails closed — the
    pipeline short-circuits before signals/LLM, writes a daily-fallback
    next_run (empty next_run_at), and logs status=skipped_clock_error so a
    transient broker outage is distinguishable from a genuine closed market."""
    from lib import market_gate as mg
    monkeypatch.setattr(
        orchestrator.market_gate, "check",
        lambda broker: mg.MarketState(
            is_open=False, next_open=None,
            rationale="test: broker clock fetch failed; failing closed",
            clock_error=True,
        ),
    )

    def _boom(*a, **kw):
        raise AssertionError("LLM should not be called when failing closed on a clock error")
    monkeypatch.setattr(orchestrator.llm, "structured_call", _boom)

    class _StubBroker:
        pass

    result = orchestrator.run_pipeline(dry_run=False, broker=_StubBroker())
    assert result["market_gate"]["is_open"] is False

    rdir = state.RUNS_DIR / result["run_id"]
    files = {p.name for p in rdir.iterdir() if p.is_file()}
    assert "market_gate.json" in files
    assert "signals.json" not in files
    assert "view.json" not in files

    # next_run schedules a near-future retry (not empty) and flags clock_error
    # so a transient broker-clock outage doesn't suppress the rest of the day.
    nr = json.loads(state.NEXT_RUN.read_text())
    assert nr["next_run_at"] != ""
    assert nr["clock_error"] is True

    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["stage"] == "market_gate"
    assert row["status"] == "skipped_clock_error"


# ---- v2 winrate features: cycle dedup, critic, state awareness ----


def test_cycle_dedup_skips_when_signals_and_positions_unchanged(tmp_state, monkeypatch):
    """After one successful cycle, a second dry-run with identical
    signals + positions should reuse the prior portfolio (dedup).

    Dry-run normally skips the dedup branch (it's gated on
    `not dry_run`). To exercise it we make dry_run produce a non-
    dry-run path via a stubbed pipeline: easier path is to manually
    write last_cycle_hash.json + current_portfolio.json and confirm
    the dedup helpers find them.
    """
    # Bootstrap state: write last_cycle_hash matching what the helper
    # would compute, plus a current_portfolio.json.
    signals_out = {"tickers": [
        {"symbol": "TQQQ", "last_close": 72.0, "momentum_30d_pct": 8.0,
         "momentum_60d_pct": 12.0, "hv_30d_annualised": 0.4,
         "hv_90d_annualised": 0.35, "dist_from_50d_ma_pct": 4.0,
         "dist_from_200d_ma_pct": 15.0},
    ]}
    positions = [{"symbol": "TQQQ", "qty": 4.0}]
    cooldown = {"SQQQ": "2026-05-26T15:00:00Z"}
    state.write_json(state.LAST_CYCLE_HASH, {
        "signals_fingerprint": orchestrator._signals_fingerprint(signals_out),
        "positions_fingerprint": orchestrator._positions_fingerprint(positions),
        "cooldown_fingerprint": orchestrator._cooldown_fingerprint(cooldown),
        "updated_at": state.utcnow_iso(),
    })
    state.write_json(state.CURRENT_PORTFOLIO, {
        "run_id": "prior", "positions": [], "all_cash": True,
    })
    result = orchestrator._check_cycle_dedup(signals_out, positions, cooldown)
    assert result is not None
    assert "portfolio" in result


def test_cycle_dedup_does_not_skip_when_cooldown_expires(tmp_state):
    """Cooldown membership shrinks with time alone. When a symbol drops out
    of cooldown but signals + positions are unchanged, dedup must NOT reuse
    the cached portfolio — the constructor needs to reconsider the re-entry.
    """
    signals_out = {"tickers": [{"symbol": "TQQQ", "last_close": 72.0}]}
    positions = [{"symbol": "TQQQ", "qty": 4.0}]
    state.write_json(state.LAST_CYCLE_HASH, {
        "signals_fingerprint": orchestrator._signals_fingerprint(signals_out),
        "positions_fingerprint": orchestrator._positions_fingerprint(positions),
        "cooldown_fingerprint": orchestrator._cooldown_fingerprint(
            {"SQQQ": "2026-05-20T15:00:00Z"}
        ),
        "updated_at": state.utcnow_iso(),
    })
    state.write_json(state.CURRENT_PORTFOLIO, {"positions": [], "all_cash": True})
    # SQQQ has since aged out of cooldown → empty cooldown set this cycle.
    result = orchestrator._check_cycle_dedup(signals_out, positions, {})
    assert result is None


def test_cycle_dedup_does_not_skip_when_signals_change(tmp_state):
    """Different signals fingerprint → dedup must NOT fire."""
    state.write_json(state.LAST_CYCLE_HASH, {
        "signals_fingerprint": "stale-hash",
        "positions_fingerprint": "stale-hash",
        "updated_at": state.utcnow_iso(),
    })
    state.write_json(state.CURRENT_PORTFOLIO, {"positions": []})
    result = orchestrator._check_cycle_dedup(
        {"tickers": [{"symbol": "TQQQ", "last_close": 72.0}]},
        [],
    )
    assert result is None


def test_cycle_dedup_first_cycle_no_hash_file(tmp_state):
    """No last_cycle_hash.json → dedup returns None (don't skip)."""
    result = orchestrator._check_cycle_dedup({"tickers": []}, [])
    assert result is None


def test_signals_fingerprint_stable_across_identical_inputs(tmp_state):
    """Same numeric content → same fingerprint regardless of generated_at."""
    a = {"tickers": [{"symbol": "TQQQ", "last_close": 72.45,
                       "momentum_30d_pct": 8.4, "momentum_60d_pct": 12.1,
                       "hv_30d_annualised": 0.42, "hv_90d_annualised": 0.38,
                       "dist_from_50d_ma_pct": 4.2,
                       "dist_from_200d_ma_pct": 15.7}]}
    b = dict(a)
    assert orchestrator._signals_fingerprint(a) == orchestrator._signals_fingerprint(b)


def test_signals_fingerprint_differs_when_momentum_moves(tmp_state):
    a = {"tickers": [{"symbol": "TQQQ", "momentum_30d_pct": 8.4}]}
    b = {"tickers": [{"symbol": "TQQQ", "momentum_30d_pct": 9.1}]}
    assert orchestrator._signals_fingerprint(a) != orchestrator._signals_fingerprint(b)


def test_signals_fingerprint_changes_when_new_macro_event_enters_window(tmp_state):
    """Codex P1 regression: an FOMC/CPI/NFP/PCE moving into the 7-day
    window MUST invalidate the dedup. Otherwise the agent skips the
    cycle exactly when new event risk appears — the opposite of what
    the event-aware signals were added for.

    Before the fix, the fingerprint only hashed numeric features and
    these two signals payloads (identical numerics, different events)
    would produce the same hash.
    """
    quiet = {"tickers": [{
        "symbol": "SPY", "last_close": 540.0,
        "momentum_30d_pct": 2.1, "momentum_60d_pct": 3.1,
        "hv_30d_annualised": 0.11, "hv_90d_annualised": 0.10,
        "dist_from_50d_ma_pct": 1.0, "dist_from_200d_ma_pct": 4.1,
        "upcoming_macro_events_7d": [],
    }]}
    with_fomc = {"tickers": [{
        "symbol": "SPY", "last_close": 540.0,
        "momentum_30d_pct": 2.1, "momentum_60d_pct": 3.1,
        "hv_30d_annualised": 0.11, "hv_90d_annualised": 0.10,
        "dist_from_50d_ma_pct": 1.0, "dist_from_200d_ma_pct": 4.1,
        "upcoming_macro_events_7d": [
            {"date": "2026-05-15", "type": "FOMC", "description": "FOMC"},
        ],
    }]}
    assert orchestrator._signals_fingerprint(quiet) != orchestrator._signals_fingerprint(with_fomc), (
        "dedup hash must invalidate when a new macro event enters the 7-day "
        "window — otherwise the agent skips strategist/construct exactly when "
        "event risk appears."
    )


def test_signals_fingerprint_stable_when_events_reordered(tmp_state):
    """Two events on different dates returned in different order
    produce the same hash — the events list is canonically sorted by
    (date, type) before hashing."""
    a = {"tickers": [{
        "symbol": "SPY", "upcoming_macro_events_7d": [
            {"date": "2026-05-15", "type": "FOMC"},
            {"date": "2026-05-14", "type": "CPI"},
        ],
    }]}
    b = {"tickers": [{
        "symbol": "SPY", "upcoming_macro_events_7d": [
            {"date": "2026-05-14", "type": "CPI"},
            {"date": "2026-05-15", "type": "FOMC"},
        ],
    }]}
    assert orchestrator._signals_fingerprint(a) == orchestrator._signals_fingerprint(b)


def test_positions_fingerprint_changes_when_qty_changes(tmp_state):
    a = [{"symbol": "TQQQ", "qty": 4.0}]
    b = [{"symbol": "TQQQ", "qty": 5.0}]
    assert orchestrator._positions_fingerprint(a) != orchestrator._positions_fingerprint(b)


def test_recent_pnl_history_empty_when_no_runs(tmp_state):
    """No nav_history rows → empty list."""
    assert orchestrator._recent_pnl_history() == []


def test_recent_pnl_history_returns_chronological_with_realized(tmp_state):
    """Two nav rows → one PnL row (curr/prev change)."""
    state.append_nav({"run_id": "r1", "at": "2026-05-13T14:00:00Z",
                      "nav_usd": 2500.0, "positions_count": 0, "all_cash": True,
                      "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0})
    state.append_nav({"run_id": "r2", "at": "2026-05-13T18:00:00Z",
                      "nav_usd": 2550.0, "positions_count": 2, "all_cash": False,
                      "gross_pnl_usd": 50.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 50.0})
    rows = orchestrator._recent_pnl_history(limit=5)
    assert len(rows) == 1
    assert rows[0]["realized_pnl_pct"] == 2.0  # (2550/2500 - 1) × 100


def test_peak_nav_30d_returns_max_observed(tmp_state):
    for nav in (2500.0, 2700.0, 2400.0):
        state.append_nav({"run_id": "r", "at": "2026-05-13T14:00:00Z",
                          "nav_usd": nav, "positions_count": 0, "all_cash": True,
                          "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0})
    assert orchestrator._peak_nav_30d() == 2700.0


def test_peak_nav_30d_ignores_rows_older_than_30_days(tmp_state, monkeypatch):
    """Codex P2 regression: a peak from 60 days ago must NOT be returned
    as the 30-day peak, regardless of how few rows are in the log.

    Earlier version used limit=180 as a proxy for 30 days, which broke
    when cadence ran faster than 6/day. Now the filter is timestamp-
    based against utcnow() - 30 days.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(state, "utcnow", lambda: now)

    # 60 days old — outside the window. Big NAV that should NOT be
    # returned as the peak.
    state.append_nav({"run_id": "old", "at": "2026-03-14T14:00:00Z",
                      "nav_usd": 5000.0, "positions_count": 0, "all_cash": True,
                      "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0})
    # 10 days old — inside the window.
    state.append_nav({"run_id": "recent", "at": "2026-05-03T14:00:00Z",
                      "nav_usd": 2700.0, "positions_count": 0, "all_cash": True,
                      "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0})
    # 1 day old — inside the window.
    state.append_nav({"run_id": "today", "at": "2026-05-12T14:00:00Z",
                      "nav_usd": 2400.0, "positions_count": 0, "all_cash": True,
                      "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0})

    # Peak should be 2700 (the 10-day-old row), NOT 5000 (the 60-day-old).
    assert orchestrator._peak_nav_30d() == 2700.0


def test_peak_nav_30d_handles_hourly_cadence(tmp_state, monkeypatch):
    """Codex P2 scenario: hourly cadence means the cap-bump 30d window
    could contain ~720 rows. With the old limit=180, only the most
    recent 7.5 days would be considered. Confirm the timestamp-based
    filter handles a high-density NAV log correctly.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(state, "utcnow", lambda: now)

    # 25 days of hourly NAV rows. Peak at hour 1 (oldest in-window).
    base = now - timedelta(days=25)
    for h in range(25 * 24):
        ts = base + timedelta(hours=h)
        # 3000 is the peak; everything else is 2500.
        nav = 3000.0 if h == 1 else 2500.0
        state.append_nav({
            "run_id": f"r{h}",
            "at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nav_usd": nav,
            "positions_count": 0, "all_cash": True,
            "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0,
        })
    # All 600 rows are within the 30-day window, including the 3000 peak.
    assert orchestrator._peak_nav_30d() == 3000.0


# ---- Phase 2: orchestrator skips opens when the daily-DD breaker is active ----


def test_stage_execute_skips_opens_when_dd_halt_active(tmp_state, monkeypatch):
    """When dd_halt is active, stage_execute submits closes (de-risking) but
    skips new opens for the UTC day. Closes still go through."""
    from lib.broker import BrokerPosition, OrderResult

    monkeypatch.setenv("ORDERS_ENABLED", "true")
    # Avoid the meta-scheduler LLM call.
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda *, ctx, portfolio, view: ("2026-05-28T20:00:00Z", "stub", "trade"),
    )
    state.set_dd_halt(dd_pct=10.0, sod_nav=2500.0, current_nav=2250.0)

    submitted: list = []

    class _FakeBroker:
        def get_positions(self):
            # Broker holds SQQQ (a close target); target portfolio wants TQQQ (an open).
            return [BrokerPosition(
                symbol="SQQQ", qty=10, avg_cost=40.0, market_value=400.0,
                unrealized_pl_usd=0.0, asset_class="us_equity",
            )]

        def submit_order(self, req):
            submitted.append((req.symbol, req.side, req.qty))
            return OrderResult(
                broker_order_id="1", symbol=req.symbol, qty=req.qty,
                side=req.side, submitted_at="", status="accepted",
            )

    portfolio = {
        "run_id": "r-dd", "nav_usd": 2500.0, "cash_usd": 100.0, "all_cash": False,
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 11.0,
        }],
    }
    ctx = orchestrator.StageContext(run_id="r-dd", dry_run=False, broker=_FakeBroker())
    next_run = orchestrator.stage_execute(ctx, portfolio, {"candidates": []})

    assert next_run["dd_halt"]["active"] is True
    assert next_run["order_plan"]["opens"] == 0          # TQQQ open skipped
    assert next_run["order_plan"]["closes"] == 1         # SQQQ close kept
    # Only the SQQQ sell (close) was actually submitted; no TQQQ buy.
    assert submitted == [("SQQQ", "sell", 10)]


def test_stage_execute_allows_derisking_reduction_during_dd_halt(tmp_state, monkeypatch):
    """Codex P1 (PR #98): a same-sign REDUCTION (long 10 -> long 5 = sell 5)
    lands in plan.requests, not closes. During a DD halt it must still be
    submitted (it de-risks); only BUYs are skipped."""
    from lib.broker import BrokerPosition, OrderResult

    monkeypatch.setenv("ORDERS_ENABLED", "true")
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda *, ctx, portfolio, view: ("2026-05-28T20:00:00Z", "stub", "trade"),
    )
    state.set_dd_halt(dd_pct=10.0, sod_nav=2500.0, current_nav=2250.0)

    submitted: list = []

    class _FakeBroker:
        def get_positions(self):
            return [BrokerPosition(
                symbol="TQQQ", qty=10, avg_cost=70.0, market_value=700.0,
                unrealized_pl_usd=0.0, asset_class="us_equity",
            )]

        def submit_order(self, req):
            submitted.append((req.symbol, req.side, req.qty))
            return OrderResult(
                broker_order_id="1", symbol=req.symbol, qty=req.qty,
                side=req.side, submitted_at="", status="accepted",
            )

    portfolio = {
        "run_id": "r-dd2", "nav_usd": 2500.0, "cash_usd": 100.0, "all_cash": False,
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 5, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 14.0,
        }],
    }
    ctx = orchestrator.StageContext(run_id="r-dd2", dry_run=False, broker=_FakeBroker())
    next_run = orchestrator.stage_execute(ctx, portfolio, {"candidates": []})

    assert next_run["dd_halt"]["active"] is True
    # The reduction (sell 5) is de-risking and must go through during a halt.
    assert ("TQQQ", "sell", 5) in submitted


def test_stage_execute_fails_closed_when_get_positions_raises(tmp_state, monkeypatch):
    """Fail-closed (Issue 5): if get_positions() raises, stage_execute must NOT
    build or submit any plan — planning opens against an assumed-empty account
    would double exposure on top of whatever is actually held. It still writes
    an empty orders.json + next_run carrying the skip reason, and schedules the
    next cycle."""
    monkeypatch.setenv("ORDERS_ENABLED", "true")
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda *, ctx, portfolio, view: ("2026-05-28T20:00:00Z", "stub", "trade"),
    )

    submitted: list = []

    class _FakeBroker:
        def get_positions(self):
            raise RuntimeError("alpaca 500")

        def submit_order(self, req):  # pragma: no cover - must never be called
            submitted.append(req)
            raise AssertionError("submit_order must not be called on get_positions failure")

    portfolio = {
        "run_id": "r-fc", "nav_usd": 2500.0, "cash_usd": 100.0, "all_cash": False,
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 11.0,
        }],
    }
    ctx = orchestrator.StageContext(run_id="r-fc", dry_run=False, broker=_FakeBroker())
    next_run = orchestrator.stage_execute(ctx, portfolio, {"candidates": []})

    assert submitted == [], "no orders should be submitted when positions can't be read"
    assert "order_plan_error" in next_run
    assert next_run["orders_skipped_reason"] == "get_positions failed — failing closed"
    assert "order_plan" not in next_run, "no plan should be built on the fail-closed path"
    # Signals run_pipeline to NOT publish the unexecuted target (Codex P1).
    assert next_run["current_portfolio_unreconciled"] is True

    orders_json = json.loads((state.run_dir("r-fc") / "orders.json").read_text())
    assert orders_json["order_ids"] == []
    # The next cycle is still scheduled.
    assert next_run["next_run_at"] == "2026-05-28T20:00:00Z"
    # Codex P2: no NAV-history row is written for an unreconciled cycle — the
    # target was never executed, so recording it as held would corrupt the
    # equity curve + strategist PnL feedback.
    assert state.read_nav_history() == []


def test_publishable_portfolio_strips_non_universe_and_option_positions():
    """Codex P2: current_portfolio.json must never record a position the order
    layer would refuse. _publishable_portfolio drops option-shaped + non-universe
    positions and recomputes all_cash when nothing tradable remains."""
    portfolio = {
        "all_cash": False, "cash_usd": 100.0,
        "positions": [
            {"kind": "etf", "symbol": "TQQQ", "shares": 4, "position_pct": 10.0},
            {"kind": "etf", "symbol": "SPY", "shares": 5, "position_pct": 10.0},   # non-universe
            {"kind": "option", "underlying": "QQQ", "strike": 400.0},               # option-shaped
        ],
    }
    cleaned = orchestrator._publishable_portfolio(portfolio)
    assert [p["symbol"] for p in cleaned["positions"]] == ["TQQQ"]
    assert cleaned["all_cash"] is False  # TQQQ remains
    # Original object is not mutated.
    assert len(portfolio["positions"]) == 3

    # All-untradable → empty + all_cash flipped True.
    only_bad = {"all_cash": False, "positions": [
        {"kind": "etf", "symbol": "TSLA", "shares": 1, "position_pct": 5.0},
    ]}
    cleaned2 = orchestrator._publishable_portfolio(only_bad)
    assert cleaned2["positions"] == []
    assert cleaned2["all_cash"] is True

    # Clean portfolio is returned unchanged (same object).
    clean = {"all_cash": False, "positions": [
        {"kind": "etf", "symbol": "SOXL", "shares": 2, "position_pct": 8.0},
    ]}
    assert orchestrator._publishable_portfolio(clean) is clean


def test_fail_closed_positions_read_preserves_current_portfolio(tmp_state, monkeypatch):
    """Codex P1: when broker.get_positions() raises during execution, the
    pipeline must NOT overwrite current_portfolio.json with the unexecuted
    target (nor advance the dedup hash) — otherwise the monitor would treat
    unfilled targets as held and real holdings as orphans, dropping their
    kill conditions. The prior current_portfolio.json is preserved."""
    from lib.llm import CallUsage, StructuredCallResult

    fixtures = {
        "signals": json.loads((Path(__file__).parent / "fixtures" / "signals.json").read_text()),
        "view": json.loads((Path(__file__).parent / "fixtures" / "view.json").read_text()),
        "portfolio": json.loads((Path(__file__).parent / "fixtures" / "portfolio.json").read_text()),
    }

    def fake_call(call, **kwargs):
        if call.stage == "strategist":
            payload = fixtures["view"]
        elif call.stage == "construct":
            payload = fixtures["portfolio"]
        elif call.stage == "critic":
            payload = {"accept": True, "critique": "ok", "suggested_changes": []}
        else:
            payload = {}
        return StructuredCallResult(
            payload=payload, usage=CallUsage(0, 0, 0, 0), cost_usd=0.0,
            cache_hit_pct=0.0, raw_text=json.dumps(payload),
        )

    monkeypatch.setenv("ORDERS_ENABLED", "true")
    # tmp_state doesn't redirect LAST_CYCLE_HASH; point it at the temp dir so
    # the dedup check starts clean (no prior hash → no skip) and the post-run
    # assertion is meaningful.
    monkeypatch.setattr(state, "LAST_CYCLE_HASH", tmp_state / "last_cycle_hash.json")
    monkeypatch.setattr(orchestrator.llm, "structured_call", fake_call)
    monkeypatch.setattr(
        orchestrator.signals, "compute_signals",
        lambda *, run_id, symbols=None: fixtures["signals"],
    )
    from lib import market_gate as mg
    monkeypatch.setattr(
        orchestrator.market_gate, "check",
        lambda broker: mg.MarketState(is_open=True, next_open=None, rationale="open"),
    )
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda *, ctx, portfolio, view: ("2026-05-28T20:00:00Z", "stub", "trade"),
    )

    # Pre-seed a prior current_portfolio.json with a sentinel marker.
    prior = {"run_id": "prior", "sentinel": True, "positions": [], "all_cash": True}
    state.write_json(state.CURRENT_PORTFOLIO, prior)

    class _RaisingBroker:
        @property
        def name(self): return "raising"
        def get_account(self): raise NotImplementedError
        def get_positions(self): raise RuntimeError("alpaca 500")
        def submit_order(self, req): raise AssertionError("must not submit")
        def cancel_all(self): return 0
        def flatten(self, sym): return None

    orchestrator.run_pipeline(dry_run=False, broker=_RaisingBroker())

    # current_portfolio.json is UNCHANGED — the prior sentinel is preserved.
    preserved = json.loads(state.CURRENT_PORTFOLIO.read_text())
    assert preserved.get("sentinel") is True
    assert preserved["run_id"] == "prior"
    # The dedup hash was not advanced (no last_cycle_hash written this cycle).
    assert not state.LAST_CYCLE_HASH.exists()


def test_sync_fills_before_cooldown_noop_in_dry_run(monkeypatch):
    """Dry-run must never hit the broker — the pre-cooldown sync is skipped."""
    called = {"n": 0}

    import lib.trades_sync as ts

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not be called in dry-run")

    monkeypatch.setattr(ts, "sync_fills_from_alpaca", _boom)
    ctx = orchestrator.StageContext(run_id="r", dry_run=True, broker=None)
    assert orchestrator._sync_fills_before_cooldown(ctx) is None
    assert called["n"] == 0


def test_sync_fills_before_cooldown_returns_error_string_on_failure(monkeypatch):
    """A sync failure is non-fatal: it returns an error string so the cycle
    can continue (and surface it on next_run) rather than abort."""
    import lib.trades_sync as ts

    monkeypatch.setattr(ts, "order_id_to_run_id_from_runs", lambda: {})
    monkeypatch.setattr(
        ts, "sync_fills_from_alpaca",
        lambda **k: (_ for _ in ()).throw(RuntimeError("alpaca down")),
    )

    class _Broker:
        _client = object()

    ctx = orchestrator.StageContext(run_id="r", dry_run=False, broker=_Broker())
    err = orchestrator._sync_fills_before_cooldown(ctx)
    assert err is not None
    assert "alpaca down" in err
