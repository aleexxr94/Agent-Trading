"""End-to-end dry-run test — acceptance criterion #2.

Runs the orchestrator pipeline against canned fixtures, asserts:
  - all 5 stage artifacts written under state/runs/{run_id}/
  - decision log has one entry per stage with status=ok and risk_warning set
  - no orders submitted (broker is None / unused)
  - schemas validate every artifact that has one
  - halt-flag stops the run before any artifact is written
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator
from lib import state


def test_dry_run_writes_all_artifacts(tmp_state):
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid

    expected = {
        "screen.json", "research.json", "chains.json",
        "scenarios.json", "portfolio.json", "sanity.json", "next_run.json",
    }
    assert {p.name for p in rdir.iterdir()} == expected

    # Schemas were validated on write — re-validate to be explicit
    state.validate(json.loads((rdir / "research.json").read_text()), "research.schema.json")
    state.validate(json.loads((rdir / "scenarios.json").read_text()), "scenarios.schema.json")
    state.validate(json.loads((rdir / "portfolio.json").read_text()), "portfolio.schema.json")
    state.validate(json.loads((rdir / "sanity.json").read_text()), "sanity.schema.json")


def test_dry_run_emits_decision_log(tmp_state):
    orchestrator.run_pipeline(dry_run=True)
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert len(lines) == 6
    stages = [json.loads(line)["stage"] for line in lines]
    # Phase 9b: chains stage runs between research and scenarios so the
    # scenarios prompt gets real bid/ask/IV/delta instead of priors.
    assert stages == ["screen", "research", "chains", "scenarios", "construct", "execute"]
    for line in lines:
        row = json.loads(line)
        assert row["status"] == "ok"
        assert row["risk_warning"]


def test_dry_run_does_not_write_current_portfolio(tmp_state):
    orchestrator.run_pipeline(dry_run=True)
    assert not state.CURRENT_PORTFOLIO.exists()


def test_live_mode_writes_current_portfolio_with_mocked_llm(tmp_state, monkeypatch):
    """Live path (dry_run=False) used to be a stub that read fixtures; it now
    calls the LLM. Mock structured_call to return canned fixture payloads."""
    fixtures = {
        "screen": json.loads((Path(__file__).parent / "fixtures" / "screen.json").read_text()),
        "scenarios": json.loads((Path(__file__).parent / "fixtures" / "scenarios.json").read_text()),
        "portfolio": json.loads((Path(__file__).parent / "fixtures" / "portfolio.json").read_text()),
    }

    def fake_call(call, **kwargs):
        from lib.llm import CallUsage, StructuredCallResult
        if call.stage == "screen":
            payload = fixtures["screen"]
        elif call.stage.startswith("research"):
            payload = {"thesis": "x", "key_drivers": ["a"], "counterarguments": ["b"], "confidence": 0.5}
        elif call.stage == "scenarios":
            payload = fixtures["scenarios"]
        elif call.stage == "construct":
            payload = fixtures["portfolio"]
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
    # Live mode now calls market_data.universe_snapshot() before the screener
    # LLM call. Stub it so the test doesn't hit the network.
    monkeypatch.setattr(
        orchestrator.market_data, "universe_snapshot",
        lambda symbols, run_id=None, **kw: [
            {"symbol": s, "kind": "etf", "last_close": 50.0, "adv_30d": 5_000_000,
             "hv_30d_annualised": 0.4} for s in symbols
        ],
    )
    # Phase 9b: chains stage runs before scenarios on live. Force it to
    # the no-data branch so the test doesn't try to hit Alpaca.
    monkeypatch.setattr(
        orchestrator, "_option_underlyings_from_research", lambda research: [],
    )
    orchestrator.run_pipeline(dry_run=False)
    assert state.CURRENT_PORTFOLIO.exists()
    p = json.loads(state.CURRENT_PORTFOLIO.read_text())
    assert (1 <= len(p["positions"]) <= 12) or p["all_cash"]


def _row(symbol: str, kind: str) -> dict:
    return {"symbol": symbol, "kind": kind, "factor": "x", "adv": 0, "hv_annualised": 0.4}


def test_select_research_candidates_prioritises_options_over_etfs():
    """Regression for the bug observed after PR #39 landed: screener passed
    SPY/QQQ/TLT plus 9 ETFs (12 total), but `[:8]` slice took the 9 ETFs
    first and dropped the option underlyings before research, so the
    constructor never saw them. Long puts couldn't fire because nothing
    asked the LLM to model them. Option underlyings must always survive
    the cap."""
    passed = [
        _row("TQQQ", "etf"), _row("UPRO", "etf"), _row("TNA", "etf"),
        _row("TZA",  "etf"), _row("SOXL", "etf"), _row("SOXS", "etf"),
        _row("LABD", "etf"), _row("BOIL", "etf"), _row("BITX", "etf"),
        _row("SPY",  "option_underlying"),
        _row("QQQ",  "option_underlying"),
        _row("TLT",  "option_underlying"),
    ]
    selected = orchestrator._select_research_candidates(passed)
    symbols = [c["symbol"] for c in selected]
    # All 3 option underlyings must be present
    assert {"SPY", "QQQ", "TLT"}.issubset(symbols), (
        f"option underlyings dropped from research input: {symbols}"
    )
    # Total cap respected
    assert len(selected) == orchestrator.RESEARCH_CANDIDATE_CAP


def test_select_research_candidates_caps_options_at_5():
    """If the screener somehow passes more than 5 option underlyings, cap
    at 5 so ETFs still get represented."""
    passed = (
        [_row(f"OPT{i}", "option_underlying") for i in range(7)]
        + [_row("TQQQ", "etf"), _row("UPRO", "etf")]
    )
    selected = orchestrator._select_research_candidates(passed)
    n_options = sum(1 for c in selected if c["kind"] == "option_underlying")
    assert n_options == 5
    assert len(selected) == 7  # 5 options + 2 etfs


def test_select_research_candidates_empty_input():
    assert orchestrator._select_research_candidates([]) == []


def test_select_research_candidates_etfs_only_uses_full_cap():
    """No option underlyings in input → fall back to top-N ETFs filling the
    full RESEARCH_CANDIDATE_CAP — don't artificially shrink the slice."""
    passed = [_row(f"ETF{i}", "etf") for i in range(15)]
    selected = orchestrator._select_research_candidates(passed)
    assert len(selected) == orchestrator.RESEARCH_CANDIDATE_CAP
    assert all(c["kind"] == "etf" for c in selected)


def test_screener_strips_markdown_fences_from_haiku_output(tmp_state, monkeypatch):
    """Regression: Haiku occasionally wraps its JSON in ```json fences despite
    the 'no markdown fences' prompt directive. Bare json.loads chokes and the
    screener silently buries every candidate in a `raw` envelope — downstream
    stages see {"passed": []} and the agent abstains to all-cash even though
    real candidates were found. Observed in first live paper run.
    """
    fence_wrapped = (
        "```json\n"
        '{"generated_at": "2026-05-11T22:18:20Z", "universe_size": 1, '
        '"passed": [{"symbol": "TQQQ", "kind": "etf", "leverage_factor": 3.0, '
        '"adv": 84130606, "hv_annualised": 0.5098}], "rejected": []}'
        "\n```"
    )

    def fake_call(call, **kwargs):
        from lib.llm import CallUsage, StructuredCallResult
        return StructuredCallResult(
            payload={}, usage=CallUsage(0, 0, 0, 0), cost_usd=0.0,
            cache_hit_pct=0.0, raw_text=fence_wrapped,
        )

    monkeypatch.setattr(orchestrator.llm, "structured_call", fake_call)
    monkeypatch.setattr(
        orchestrator.market_data, "universe_snapshot",
        lambda symbols, run_id=None, **kw: [],
    )
    ctx = orchestrator.StageContext(run_id="t", dry_run=False, broker=None)
    out = orchestrator.stage_screen(ctx)
    assert len(out["passed"]) == 1
    assert out["passed"][0]["symbol"] == "TQQQ"
    # And the unused `raw` fallback envelope is absent on the happy path.
    assert "raw" not in out


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
    """systemd-run refuses to schedule into the past. The stub had been
    emitting the current UTC time, which produced the operator's
    'Could not schedule transient timer' warning. Verify _default_next_run_at
    always pushes ≥ a couple of hours forward."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    for all_cash in (True, False):
        at = orchestrator._default_next_run_at({"all_cash": all_cash, "positions": []})
        parsed = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert parsed > now + timedelta(hours=3), (
            f"next_run_at {at} not far enough in the future (all_cash={all_cash})"
        )


def test_run_pipeline_writes_future_next_run_at(tmp_state):
    """End-to-end: state/next_run.json carries a future timestamp after a live
    (non-dry-run) call. Uses mocked LLM + universe."""
    fixtures = {
        "portfolio": json.loads((Path(__file__).parent / "fixtures" / "portfolio.json").read_text()),
        "scenarios": json.loads((Path(__file__).parent / "fixtures" / "scenarios.json").read_text()),
        "screen": json.loads((Path(__file__).parent / "fixtures" / "screen.json").read_text()),
    }

    def fake_call(call, **kw):
        from lib.llm import CallUsage, StructuredCallResult
        if call.stage == "screen":
            payload = fixtures["screen"]
        elif call.stage.startswith("research"):
            payload = {"thesis": "x", "key_drivers": ["a"], "counterarguments": ["b"], "confidence": 0.5}
        elif call.stage == "scenarios":
            payload = fixtures["scenarios"]
        elif call.stage == "construct":
            payload = fixtures["portfolio"]
        else:
            payload = {}
        return StructuredCallResult(payload=payload, usage=CallUsage(0, 0, 0, 0),
                                     cost_usd=0.0, cache_hit_pct=0.0, raw_text=json.dumps(payload))

    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(orchestrator.llm, "structured_call", fake_call)
        monkeypatch.setattr(
            orchestrator.market_data, "universe_snapshot",
            lambda symbols, run_id=None, **kw: [
                {"symbol": s, "kind": "etf", "last_close": 50.0, "adv_30d": 5_000_000,
                 "hv_30d_annualised": 0.4} for s in symbols
            ],
        )
        # Skip chain-fetch live network call — empty underlyings list.
        monkeypatch.setattr(
            orchestrator, "_option_underlyings_from_research", lambda research: [],
        )
        orchestrator.run_pipeline(dry_run=False)
    finally:
        monkeypatch.undo()

    assert state.NEXT_RUN.exists()
    nr = json.loads(state.NEXT_RUN.read_text())
    from datetime import datetime, timezone
    parsed = datetime.strptime(nr["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert parsed > datetime.now(timezone.utc), "next_run_at must be future"


# ---------- Phase 9b: chain-fetch stage ----------


def test_option_underlyings_from_research_filters_correctly():
    research = {
        "candidates": [
            {"symbol": "TQQQ", "instrument_kind": "etf", "abstain": False},
            {"symbol": "SPY", "instrument_kind": "option", "abstain": False},
            {"symbol": "QQQ", "instrument_kind": "option", "abstain": True},  # skip
            {"underlying": "TLT", "instrument_kind": "option", "abstain": False},
        ],
    }
    out = orchestrator._option_underlyings_from_research(research)
    assert out == ["SPY", "TLT"]  # abstained QQQ skipped; ETF skipped


def test_option_underlyings_from_research_empty_input():
    assert orchestrator._option_underlyings_from_research({}) == []
    assert orchestrator._option_underlyings_from_research({"candidates": []}) == []


def test_spot_lookup_from_screen_pulls_last_close():
    screen = {
        "passed": [
            {"symbol": "SPY", "last_close": 530.42},
            {"symbol": "QQQ", "last_close": 0},   # invalid spot — skipped
            {"symbol": "IWM", "last_close": None},
        ],
        "failed": [{"symbol": "TLT", "last_close": 88.5}],
    }
    out = orchestrator._spot_lookup_from_screen(screen)
    assert out == {"SPY": 530.42, "TLT": 88.5}


def test_spot_lookup_prefers_spot_prices_side_table_over_passed_rows():
    """Regression 2026-05-12T21:40 paper run: Haiku's screener output
    omitted `last_close` per passed row, so the chain stage skipped every
    fetch. Fix: stage_screen now attaches a `spot_prices` side-table
    built from the universe snapshot, and the lookup prefers it."""
    screen = {
        "spot_prices": {"SPY": 530.42, "QQQ": 440.0, "IWM": 220.0, "TLT": 88.5},
        "passed": [
            {"symbol": "SPY", "kind": "option_underlying"},  # no last_close
            {"symbol": "QQQ", "kind": "option_underlying"},
        ],
    }
    out = orchestrator._spot_lookup_from_screen(screen)
    assert out == {"SPY": 530.42, "QQQ": 440.0, "IWM": 220.0, "TLT": 88.5}


def test_spot_lookup_side_table_takes_priority_over_legacy_field():
    """When both spot_prices and per-row last_close exist, the side-table
    wins."""
    screen = {
        "spot_prices": {"SPY": 530.42},
        "passed": [{"symbol": "SPY", "last_close": 999.99}],
    }
    out = orchestrator._spot_lookup_from_screen(screen)
    assert out == {"SPY": 530.42}


def test_spot_lookup_falls_back_to_passed_rows_when_no_side_table():
    """Legacy / fixture path: an older screen artifact without the
    spot_prices side-table must still resolve spots from per-row
    last_close so back-compat doesn't break."""
    screen = {"passed": [{"symbol": "SPY", "last_close": 530.42}]}
    out = orchestrator._spot_lookup_from_screen(screen)
    assert out == {"SPY": 530.42}


def test_spot_lookup_skips_zero_and_none_in_side_table():
    screen = {
        "spot_prices": {"SPY": 530.42, "QQQ": 0, "IWM": None, "TLT": -1.0},
    }
    out = orchestrator._spot_lookup_from_screen(screen)
    assert out == {"SPY": 530.42}


def test_stage_screen_attaches_spot_prices_from_universe_snapshot(tmp_state, monkeypatch):
    """End-to-end: stage_screen builds the spot_prices side-table from the
    universe snapshot's last_close values, regardless of whether Haiku
    echoes them in its JSON output."""
    snapshot = [
        {"symbol": "SPY", "kind": "option_underlying", "last_close": 530.42, "adv_30d": 57_667_948, "hv_30d_annualised": 0.13},
        {"symbol": "QQQ", "kind": "option_underlying", "last_close": 440.0, "adv_30d": 44_385_545, "hv_30d_annualised": 0.17},
        {"symbol": "BROKEN", "kind": "etf", "error": "no history"},  # no last_close
    ]
    monkeypatch.setattr(
        orchestrator.market_data, "universe_snapshot",
        lambda symbols, run_id=None, **kw: snapshot,
    )
    haiku_response = json.dumps({
        "generated_at": "2026-05-12T21:40:00Z",
        "passed": [
            {"symbol": "SPY", "kind": "option_underlying", "adv": 57_667_948, "hv_annualised": 0.13},
            {"symbol": "QQQ", "kind": "option_underlying", "adv": 44_385_545, "hv_annualised": 0.17},
        ],
        "rejected": [],
    })

    def fake_call(call, **kwargs):
        from lib.llm import CallUsage, StructuredCallResult
        return StructuredCallResult(
            payload={}, usage=CallUsage(0, 0, 0, 0), cost_usd=0.0,
            cache_hit_pct=0.0, raw_text=haiku_response,
        )

    monkeypatch.setattr(orchestrator.llm, "structured_call", fake_call)
    ctx = orchestrator.StageContext(run_id="t", dry_run=False, broker=None)
    out = orchestrator.stage_screen(ctx)

    assert out["spot_prices"] == {"SPY": 530.42, "QQQ": 440.0}
    spots = orchestrator._spot_lookup_from_screen(out)
    assert spots == {"SPY": 530.42, "QQQ": 440.0}


def test_stage_chains_dry_run_loads_fixture(tmp_state):
    """Dry-run reads tests/fixtures/chains.json if present."""
    ctx = orchestrator.StageContext(run_id="rid-x", dry_run=True, broker=None)
    out = orchestrator.stage_chains(ctx, research={}, screen={})
    assert out["run_id"] == "rid-x"
    assert "underlyings" in out


def test_stage_chains_skips_underlyings_with_no_spot(tmp_state, monkeypatch):
    """Live path: underlyings with no spot price get an explanatory error
    row rather than crashing the whole stage."""
    research = {"candidates": [
        {"symbol": "SPY", "instrument_kind": "option", "abstain": False},
    ]}
    screen = {"passed": [{"symbol": "SPY", "last_close": None}]}

    # Block ChainFetcher construction — we shouldn't reach it.
    class _Boom:
        def __init__(self, *a, **kw): raise AssertionError("should not construct")
    from lib import options_chain as oc
    monkeypatch.setattr(oc, "ChainFetcher", _Boom)

    ctx = orchestrator.StageContext(run_id="r", dry_run=False, broker=None)
    out = orchestrator.stage_chains(ctx, research=research, screen=screen)
    assert "error" in out["underlyings"]["SPY"]
    assert "no spot" in out["underlyings"]["SPY"]["error"]


def test_stage_chains_isolates_per_underlying_failures(tmp_state, monkeypatch):
    """One underlying's ChainFetchError must not block the other's success."""
    from lib import options_chain as oc

    research = {"candidates": [
        {"symbol": "SPY", "instrument_kind": "option", "abstain": False},
        {"symbol": "QQQ", "instrument_kind": "option", "abstain": False},
    ]}
    screen = {"passed": [
        {"symbol": "SPY", "last_close": 530.0},
        {"symbol": "QQQ", "last_close": 440.0},
    ]}

    class _MixedFetcher:
        def fetch(self, underlying, **kw):
            if underlying == "SPY":
                return {"underlying": "SPY", "spot": 530.0, "calls": [], "puts": []}
            raise oc.ChainFetchError("alpaca 503")

    monkeypatch.setattr(oc, "ChainFetcher", lambda *a, **kw: _MixedFetcher())

    ctx = orchestrator.StageContext(run_id="r", dry_run=False, broker=None)
    out = orchestrator.stage_chains(ctx, research=research, screen=screen)
    assert out["underlyings"]["SPY"]["underlying"] == "SPY"
    assert "alpaca 503" in out["underlyings"]["QQQ"]["error"]


def test_stage_chains_returns_empty_when_no_option_underlyings(tmp_state, monkeypatch):
    """No option candidates → empty underlyings map, no fetcher constructed."""
    research = {"candidates": [{"symbol": "TQQQ", "instrument_kind": "etf"}]}

    class _Boom:
        def __init__(self, *a, **kw): raise AssertionError("should not construct")
    from lib import options_chain as oc
    monkeypatch.setattr(oc, "ChainFetcher", _Boom)

    ctx = orchestrator.StageContext(run_id="r", dry_run=False, broker=None)
    out = orchestrator.stage_chains(ctx, research=research, screen={})
    assert out["underlyings"] == {}


def test_dry_run_pipeline_passes_chains_through_to_scenarios(tmp_state):
    """End-to-end: run_pipeline returns chains as a top-level key."""
    result = orchestrator.run_pipeline(dry_run=True)
    assert "chains" in result
    assert "underlyings" in result["chains"]


def test_dry_run_writes_sanity_json_with_known_status(tmp_state):
    """Sanity report is written next to portfolio.json on every run.

    The fixture portfolio's kill_conditions only carry max_loss_pct (no
    price-stop or time-stop fields), which is exactly the gap the
    kill_conditions_complete rule was built to catch. So we expect at
    least one rule to fire on the existing fixture — confirms sanity
    is actually evaluating, not just emitting an empty report.
    """
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid
    sanity_doc = json.loads((rdir / "sanity.json").read_text())
    assert sanity_doc["run_id"] == rid
    assert sanity_doc["status"] in ("pass", "warn", "fail")
    # Schema validation already enforced on write (orchestrator passes
    # schema=sanity.schema.json to state.write_json), but re-validate to
    # surface intent in the test.
    state.validate(sanity_doc, "sanity.schema.json")
    # Rule list mirrors lib/sanity.RULES — pin the count so a future
    # rule add/remove gets a heads-up in the dry-run test rather than
    # only in test_sanity.py.
    assert len(sanity_doc["rules"]) == 6


def test_sanity_block_on_fail_skips_execute_and_writes_block_reason(tmp_state, monkeypatch):
    """SANITY_BLOCK_ON_FAIL=true + fixture-known fail → stage_execute is
    skipped, next_run.json carries the sanity_block field, current
    portfolio is still written (so the dashboard can show what was
    rejected). The fixture's kill_conditions incompleteness is the
    "failing rule" trigger here — no need to construct a synthetic
    portfolio.

    Codex P1 on PR #64: the sanity-block path MUST still populate
    next_run_at with a sensible future timestamp. The root-level
    run_scheduler.sh reads `.next_run_at // empty` and skips its tick
    when empty — without this, enabling sanity-blocking would silently
    drop the orchestrator from 1-24h cadence to the ~24h daily
    fallback timer. Reuse the existing _default_next_run_at heuristic
    so a sanity-fail skips ONE cycle's execute but cadence is
    preserved.
    """
    monkeypatch.setenv("SANITY_BLOCK_ON_FAIL", "true")
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid

    sanity_doc = json.loads((rdir / "sanity.json").read_text())
    assert sanity_doc["status"] == "fail", (
        "fixture expected to fail at least one rule "
        "(kill_conditions_complete on the all-max_loss-only kill blocks)"
    )

    next_run = json.loads((rdir / "next_run.json").read_text())
    assert "sanity_block" in next_run
    assert next_run["sanity_block"]["status"] == "fail"
    assert len(next_run["sanity_block"]["failed_rules"]) >= 1

    # Cadence preserved — scheduler keeps firing.
    assert isinstance(next_run["next_run_at"], str)
    from datetime import datetime, timezone
    parsed = datetime.strptime(next_run["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    # _default_next_run_at returns 4h (positions held) or 6h (all-cash)
    # ahead. Be generous on both ends to absorb test timing.
    delta_hours = (parsed - datetime.now(timezone.utc)).total_seconds() / 3600.0
    assert 0.5 <= delta_hours <= 8.0, (
        f"next_run_at delta {delta_hours:.2f}h outside 0.5h-8h expected band; "
        f"sanity-block path must use the same heuristic as the meta-scheduler "
        f"fallback so deploy/run_scheduler.sh keeps firing."
    )


def test_sanity_pass_path_writes_summary_into_next_run(tmp_state, monkeypatch):
    """Default path (SANITY_BLOCK_ON_FAIL unset): stage_execute proceeds
    normally; next_run.json carries a `sanity` field with the rollup so
    the dashboard meter can render without re-parsing sanity.json.
    """
    monkeypatch.delenv("SANITY_BLOCK_ON_FAIL", raising=False)
    result = orchestrator.run_pipeline(dry_run=True)
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid
    next_run = json.loads((rdir / "next_run.json").read_text())
    assert "sanity" in next_run
    assert next_run["sanity"]["status"] in ("pass", "warn", "fail")
    assert set(next_run["sanity"]["summary"].keys()) == {"pass", "warn", "fail", "skip"}
    # Confirms the non-blocking path didn't trip the block branch.
    assert "sanity_block" not in next_run


def test_main_passes_broker_to_run_pipeline(tmp_state, monkeypatch):
    """Regression for the live-run bug observed May 12 2026: the agent
    constructed real positive-EV portfolios but Alpaca showed zero
    positions because main() called run_pipeline WITHOUT broker=. With
    broker defaulting to None, stage_execute's `if ctx.broker is not None`
    guard always skipped submission, no matter how many trades the
    constructor produced. This test pins that main() now constructs a
    broker (via _try_load_broker) on live runs and forwards it through."""
    captured = {}

    def fake_run_pipeline(*, dry_run, run_id=None, broker=None):
        captured["broker_arg"] = broker
        return {"run_id": "stub", "screen": {}, "research": {}, "scenarios": {}, "portfolio": {}}

    # Stub the broker loader so we don't open a real connection in the test
    class _FakeBroker: pass
    monkeypatch.setattr(orchestrator, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(orchestrator, "_try_load_broker", lambda: _FakeBroker())

    # Live run: broker must be passed
    orchestrator.main([])
    assert isinstance(captured["broker_arg"], _FakeBroker), (
        "main() did not pass a broker to run_pipeline on a non-dry-run cycle; "
        "stage_execute will silently skip order submission"
    )

    # Dry run: broker should NOT be loaded (no network connection on dry-runs)
    captured["broker_arg"] = "untouched"
    orchestrator.main(["--dry-run"])
    assert captured["broker_arg"] is None, (
        "main() must skip broker init on --dry-run; got "
        f"{captured['broker_arg']!r} — dry-runs are fixture-only"
    )
