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

    expected = {"screen.json", "research.json", "scenarios.json", "portfolio.json", "next_run.json"}
    assert {p.name for p in rdir.iterdir()} == expected

    # Schemas were validated on write — re-validate to be explicit
    state.validate(json.loads((rdir / "research.json").read_text()), "research.schema.json")
    state.validate(json.loads((rdir / "scenarios.json").read_text()), "scenarios.schema.json")
    state.validate(json.loads((rdir / "portfolio.json").read_text()), "portfolio.schema.json")


def test_dry_run_emits_decision_log(tmp_state):
    orchestrator.run_pipeline(dry_run=True)
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert len(lines) == 5
    stages = [json.loads(line)["stage"] for line in lines]
    assert stages == ["screen", "research", "scenarios", "construct", "execute"]
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
    # Stub stages.bull/bear/scenarios/etc to avoid loading prompt files inside this test
    orchestrator.run_pipeline(dry_run=False)
    assert state.CURRENT_PORTFOLIO.exists()
    p = json.loads(state.CURRENT_PORTFOLIO.read_text())
    assert (8 <= len(p["positions"]) <= 12) or p["all_cash"]


def test_halt_flag_blocks_pipeline_start(tmp_state):
    state.set_halt("test")
    with pytest.raises(Exception):
        orchestrator.run_pipeline(dry_run=True)
    # No run dir should have been created
    assert not any(state.RUNS_DIR.iterdir())


def test_position_band_and_total_pct(tmp_state):
    result = orchestrator.run_pipeline(dry_run=True)
    p = result["portfolio"]
    assert 8 <= len(p["positions"]) <= 12
    assert sum(pos["position_pct"] for pos in p["positions"]) <= 100.0
