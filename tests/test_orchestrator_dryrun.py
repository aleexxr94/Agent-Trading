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
    # Live mode now calls market_data.universe_snapshot() before the screener
    # LLM call. Stub it so the test doesn't hit the network.
    monkeypatch.setattr(
        orchestrator.market_data, "universe_snapshot",
        lambda symbols, run_id=None, **kw: [
            {"symbol": s, "kind": "etf", "last_close": 50.0, "adv_30d": 5_000_000,
             "hv_30d_annualised": 0.4} for s in symbols
        ],
    )
    orchestrator.run_pipeline(dry_run=False)
    assert state.CURRENT_PORTFOLIO.exists()
    p = json.loads(state.CURRENT_PORTFOLIO.read_text())
    assert (3 <= len(p["positions"]) <= 12) or p["all_cash"]


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
    assert 3 <= len(p["positions"]) <= 12
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
        orchestrator.run_pipeline(dry_run=False)
    finally:
        monkeypatch.undo()

    assert state.NEXT_RUN.exists()
    nr = json.loads(state.NEXT_RUN.read_text())
    from datetime import datetime, timezone
    parsed = datetime.strptime(nr["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert parsed > datetime.now(timezone.utc), "next_run_at must be future"
