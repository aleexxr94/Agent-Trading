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
    # v2 dry-run logs 4 decisions: signals + strategist + construct + execute
    # (market_gate doesn't fire in dry-run; sanity doesn't go through _run_stage).
    assert len(lines) == 4
    stages = [json.loads(line)["stage"] for line in lines]
    assert stages == ["signals", "strategist", "construct", "execute"]
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
    # Rule list mirrors lib/sanity.RULES — pin the count.
    assert len(sanity_doc["rules"]) == 6


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
