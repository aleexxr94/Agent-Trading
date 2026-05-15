"""Review-cycle pipeline tests.

Covers the after-hours reflection branch of run_pipeline:
  - review cycles run signals + strategist + meta only
  - no construct / sanity / execute → no orders, no NAV append, no
    cycle-dedup hash update
  - daily review-frequency cap blocks excess autonomous reviews from
    next_run.json but lets CLI-driven --intent=review bypass
  - meta-scheduler emits cycle_intent for the NEXT cycle
  - decision rows carry cycle_intent + intent_source so audit can
    prove which branch ran
  - return shape is discriminated by cycle_intent
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator
from lib import state


def _fake_strategist_call(call, **kw):
    """Return a canned schema-valid view payload for any LLM call.

    Review cycles only fire one LLM call (strategist); meta-scheduler
    is short-circuited via dry_run=True or by stubbing _compute_next_run_at.
    """
    from lib.llm import CallUsage, StructuredCallResult
    fixtures = Path(__file__).parent / "fixtures"
    if call.stage == "strategist":
        payload = json.loads((fixtures / "view.json").read_text())
    else:
        payload = {}
    return StructuredCallResult(
        payload=payload,
        usage=CallUsage(0, 0, 0, 0),
        cost_usd=0.05,
        cache_hit_pct=0.0,
        raw_text=json.dumps(payload),
    )


def test_review_cycle_writes_review_artifact_not_view(tmp_state):
    """Review cycle writes review.json next to signals.json; NO view.json."""
    result = orchestrator.run_pipeline(dry_run=True, cli_intent="review")
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid
    files = {p.name for p in rdir.iterdir() if p.is_file()}
    assert "review.json" in files
    assert "view.json" not in files
    assert "signals.json" in files
    assert "next_run.json" in files


def test_review_cycle_skips_construct_and_execute(tmp_state):
    """No portfolio.json, no sanity.json, no chain_lookups.json,
    no critique.json — those are trade-only stages."""
    result = orchestrator.run_pipeline(dry_run=True, cli_intent="review")
    rid = result["run_id"]
    rdir = state.RUNS_DIR / rid
    files = {p.name for p in rdir.iterdir() if p.is_file()}
    assert "portfolio.json" not in files
    assert "sanity.json" not in files
    assert "chain_lookups.json" not in files
    assert "critique.json" not in files


def test_review_cycle_bypasses_closed_market_gate(tmp_state, monkeypatch):
    """A live-mode review cycle still runs strategist when broker
    reports is_open=False. That's the whole point of the feature."""
    from lib import market_gate as mg

    # If market_gate.check were called, this would force a closed
    # short-circuit. Assert it ISN'T called by raising if it is.
    def _gate_should_not_run(broker):
        raise AssertionError("review cycle must skip market_gate")
    monkeypatch.setattr(orchestrator.market_gate, "check", _gate_should_not_run)

    # Stub LLM + signals + meta-scheduler to keep the cycle hermetic.
    monkeypatch.setattr(orchestrator.llm, "structured_call", _fake_strategist_call)
    fixtures_dir = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(
        orchestrator.signals, "compute_signals",
        lambda *, run_id, symbols=None: json.loads((fixtures_dir / "signals.json").read_text()),
    )
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda **kw: ("2026-05-16T13:30:00Z", "stub: review next-run", "trade"),
    )

    class _StubBroker:
        def get_clock(self):
            return None
        def get_positions(self):
            return []

    result = orchestrator.run_pipeline(
        dry_run=False, broker=_StubBroker(), cli_intent="review",
    )
    assert result["cycle_intent"] == "review"
    # Strategist ran → review.json exists
    rdir = state.RUNS_DIR / result["run_id"]
    assert (rdir / "review.json").exists()


def test_review_cycle_does_not_update_dedup_hash(tmp_state, monkeypatch):
    """The cycle-dedup hash must NOT update on a review cycle; a
    review followed by an unchanged-signals trade cycle would
    otherwise dedup-skip into a stale (or missing) portfolio."""
    # Seed an existing dedup hash so we can prove it's untouched.
    state.write_json(state.LAST_CYCLE_HASH, {
        "signals_fingerprint": "seed-fp",
        "positions_fingerprint": "seed-fp",
        "updated_at": state.utcnow_iso(),
    })
    before = state.LAST_CYCLE_HASH.read_text()

    monkeypatch.setattr(orchestrator.llm, "structured_call", _fake_strategist_call)
    fixtures_dir = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(
        orchestrator.signals, "compute_signals",
        lambda *, run_id, symbols=None: json.loads((fixtures_dir / "signals.json").read_text()),
    )
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda **kw: ("2026-05-16T13:30:00Z", "stub", "trade"),
    )

    class _StubBroker:
        def get_clock(self): return None
        def get_positions(self): return []

    orchestrator.run_pipeline(
        dry_run=False, broker=_StubBroker(), cli_intent="review",
    )
    after = state.LAST_CYCLE_HASH.read_text()
    assert before == after, "review cycle must not touch the dedup fingerprint"


def test_review_cycle_does_not_append_nav_history(tmp_state, monkeypatch):
    """NAV history is the trade-cycle equity curve; review cycles don't
    change positions so they don't append a row."""
    # Seed one NAV row so we can compare line counts before/after.
    state.append_nav({
        "run_id": "seed", "at": "2026-05-14T17:00:00Z", "nav_usd": 2500.0,
        "positions_count": 0, "all_cash": True,
        "gross_pnl_usd": 0.0, "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0,
    })
    before_lines = state.NAV_HISTORY_LOG.read_text().splitlines()

    monkeypatch.setattr(orchestrator.llm, "structured_call", _fake_strategist_call)
    fixtures_dir = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(
        orchestrator.signals, "compute_signals",
        lambda *, run_id, symbols=None: json.loads((fixtures_dir / "signals.json").read_text()),
    )
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda **kw: ("2026-05-16T13:30:00Z", "stub", "trade"),
    )

    class _StubBroker:
        def get_clock(self): return None
        def get_positions(self): return []

    orchestrator.run_pipeline(
        dry_run=False, broker=_StubBroker(), cli_intent="review",
    )
    after_lines = state.NAV_HISTORY_LOG.read_text().splitlines()
    assert len(after_lines) == len(before_lines), (
        "review cycle should not append a NAV history row"
    )


def test_review_cycle_decision_log_carries_intent(tmp_state):
    """Every decision row from a review cycle carries cycle_intent +
    intent_source so audit can prove which branch ran."""
    orchestrator.run_pipeline(dry_run=True, cli_intent="review")
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert lines, "expected at least one decision row"
    for line in lines:
        row = json.loads(line)
        assert row.get("cycle_intent") == "review", (
            f"row missing review intent: {row}"
        )
        assert row.get("intent_source") == "cli", (
            f"--intent=review came from CLI, expected intent_source=cli, got: {row}"
        )


def test_review_cycle_emits_review_complete_decision(tmp_state):
    """A synthetic 'review_complete' decision row marks the end of every
    review cycle — the cap counter uses this row to count cycles."""
    orchestrator.run_pipeline(dry_run=True, cli_intent="review")
    rows = [
        json.loads(l)
        for l in state.DECISIONS_LOG.read_text().strip().splitlines()
    ]
    review_complete = [r for r in rows if r["stage"] == "review_complete"]
    assert len(review_complete) == 1
    assert review_complete[0]["status"] == "ok"


def test_review_cap_blocks_autonomous_excess(tmp_state, monkeypatch):
    """With MAX_REVIEW_CYCLES_PER_DAY=1 and one autonomous review
    already logged today, a second autonomous review (intent_source=
    file) gets skipped with status=skipped_review_cap."""
    monkeypatch.setenv("MAX_REVIEW_CYCLES_PER_DAY", "1")

    # Seed one prior autonomous review row in today's log.
    today_iso = state.utcnow_iso()
    state.append_decision({
        "run_id": "prior-review",
        "stage": "review_complete",
        "model": "local-deterministic",
        "inputs_hash": "a" * 32,
        "output_ref": "review.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.05,
        "started_at": today_iso, "ended_at": today_iso,
        "status": "ok", "risk_warning": "test",
        "cycle_intent": "review", "intent_source": "file",
    })

    # Set up state.NEXT_RUN with cycle_intent=review so the orchestrator
    # picks it up as an autonomous review (intent_source=file).
    state.write_json(state.NEXT_RUN, {
        "run_id": "prior-review", "next_run_at": today_iso,
        "rationale": "test seed", "cycle_intent": "review",
    })

    # No CLI override → intent_source resolves to "file" → cap applies.
    result = orchestrator.run_pipeline(dry_run=True)
    assert result.get("review_cap_skipped") is True
    assert result["cycle_intent"] == "review"

    # The skip wrote a decision row with status=skipped_review_cap.
    rows = [
        json.loads(l)
        for l in state.DECISIONS_LOG.read_text().strip().splitlines()
    ]
    skips = [r for r in rows if r.get("status") == "skipped_review_cap"]
    assert len(skips) == 1
    assert skips[0]["stage"] == "review_complete"

    # next_run.json advances with cycle_intent=trade (no more reviews today).
    nr = json.loads(state.NEXT_RUN.read_text())
    assert nr["cycle_intent"] == "trade"
    assert nr.get("review_cap_skipped") is True


def test_review_cap_does_not_block_cli_override(tmp_state):
    """--intent=review --ignore-cap MUST run regardless of how many
    reviews have happened today. Operator manual override."""
    import os
    os.environ["MAX_REVIEW_CYCLES_PER_DAY"] = "0"
    try:
        # Even though cap=0, CLI ignore_cap=True must let it through.
        result = orchestrator.run_pipeline(
            dry_run=True, cli_intent="review", ignore_cap=True,
        )
        assert result.get("cycle_intent") == "review"
        assert "review_cap_skipped" not in result, (
            "ignore_cap=True should bypass the cap entirely"
        )
        # And it should have actually run — review.json exists.
        rid = result["run_id"]
        assert (state.RUNS_DIR / rid / "review.json").exists()
    finally:
        os.environ.pop("MAX_REVIEW_CYCLES_PER_DAY", None)


def test_review_cap_does_not_count_cli_runs(tmp_state):
    """CLI-driven reviews don't count toward the daily cap — only
    intent_source=file cycles do. Seed three CLI-driven reviews and
    confirm the counter stays at 0."""
    today_iso = state.utcnow_iso()
    for i in range(3):
        state.append_decision({
            "run_id": f"cli-{i}",
            "stage": "review_complete",
            "model": "local-deterministic",
            "inputs_hash": "a" * 32,
            "output_ref": "review.json",
            "prompt_cache_hit_pct": 0.0, "cost_usd": 0.05,
            "started_at": today_iso, "ended_at": today_iso,
            "status": "ok", "risk_warning": "test",
            "cycle_intent": "review", "intent_source": "cli",
        })
    assert orchestrator._count_autonomous_reviews_today() == 0


def test_cycle_intent_loaded_from_next_run_defaults_to_trade(tmp_state):
    """Legacy next_run.json without cycle_intent → orchestrator treats
    the cycle as trade (back-compat)."""
    state.write_json(state.NEXT_RUN, {
        "run_id": "legacy", "next_run_at": "2026-05-15T13:30:00Z",
        "rationale": "legacy run, no cycle_intent field",
    })
    intent, source = orchestrator._load_cycle_intent(
        cli_intent=None, ignore_cap=False,
    )
    assert intent == "trade"
    assert source == "default"


def test_cycle_intent_cli_overrides_next_run_file(tmp_state):
    """CLI --intent must override prior next_run.json's intent."""
    state.write_json(state.NEXT_RUN, {
        "run_id": "prior", "next_run_at": "2026-05-15T13:30:00Z",
        "rationale": "prior cycle", "cycle_intent": "review",
    })
    intent, source = orchestrator._load_cycle_intent(
        cli_intent="trade", ignore_cap=False,
    )
    assert intent == "trade"
    assert source == "cli"


def test_review_cycle_meta_scheduler_emits_intent(tmp_state, monkeypatch):
    """Meta-scheduler returns (next_run_at, rationale, intent); the
    intent is persisted into the next_run.json file so the NEXT cycle
    can read it back via _load_cycle_intent(source='file')."""
    monkeypatch.setattr(orchestrator.llm, "structured_call", _fake_strategist_call)
    fixtures_dir = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(
        orchestrator.signals, "compute_signals",
        lambda *, run_id, symbols=None: json.loads((fixtures_dir / "signals.json").read_text()),
    )
    # Stub meta to return a known intent.
    monkeypatch.setattr(
        orchestrator, "_compute_next_run_at",
        lambda **kw: ("2026-05-16T13:30:00Z", "stub: next is trade", "trade"),
    )

    class _StubBroker:
        def get_clock(self): return None
        def get_positions(self): return []

    result = orchestrator.run_pipeline(
        dry_run=False, broker=_StubBroker(), cli_intent="review",
    )
    nr = result["next_run"]
    assert nr["cycle_intent"] == "trade"
    assert nr["next_run_at"] == "2026-05-16T13:30:00Z"
    # And the on-disk file matches (back-compat: the NEXT cycle reads
    # NEXT_RUN to pick its intent).
    on_disk = json.loads(state.NEXT_RUN.read_text())
    assert on_disk["cycle_intent"] == "trade"


def test_review_cycle_return_shape_discriminated(tmp_state):
    """Review-cycle return dict carries cycle_intent: 'review' and
    omits trade-only keys (portfolio, sanity)."""
    result = orchestrator.run_pipeline(dry_run=True, cli_intent="review")
    assert result["cycle_intent"] == "review"
    assert "portfolio" not in result
    assert "sanity" not in result
    assert "review" in result
    assert "signals" in result
    assert "next_run" in result


def test_trade_cycle_decision_rows_carry_trade_intent(tmp_state):
    """Regression: trade cycles MUST stamp cycle_intent=trade on every
    decision row. If they stamped 'review' or omitted it, audit could
    not distinguish branches."""
    orchestrator.run_pipeline(dry_run=True)
    rows = [
        json.loads(l)
        for l in state.DECISIONS_LOG.read_text().strip().splitlines()
    ]
    assert rows, "expected at least one trade-cycle decision row"
    for r in rows:
        assert r["cycle_intent"] == "trade", (
            f"trade-cycle row stamped wrong intent: {r}"
        )
        # No CLI flag passed → source must be 'default'.
        assert r["intent_source"] == "default"
