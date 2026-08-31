"""bin/analyze_runs — cycle-kind labeling, review-artifact enrichment,
and 0.0-vs-missing rendering.

Review cycles write review.json (not view.json), market-closed cycles
write only market_gate.json + next_run.json, dedup cycles only signals +
next_run — all three used to render as blank crash-lookalike rows. Rows
now carry cycle_intent (joined from decisions.jsonl via
dashboard_data.intent_by_run) and a display `kind` with precedence
crash → closed → dedup → review/trade.
"""
from __future__ import annotations

import json

from bin import analyze_runs
from lib import state


def _mk_run(rid: str, files: dict[str, dict]) -> None:
    d = state.RUNS_DIR / rid
    d.mkdir(parents=True)
    for name, payload in files.items():
        (d / name).write_text(json.dumps(payload))


def _decision(rid: str, stage: str, intent: str, source: str = "file") -> None:
    state.append_decision({
        "run_id": rid, "stage": stage, "model": "m",
        "inputs_hash": "deadbeefcafebabe", "output_ref": f"{stage}.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": state.utcnow_iso(), "ended_at": state.utcnow_iso(),
        "status": "ok", "risk_warning": "PAPER TRADING — high risk.",
        "cycle_intent": intent, "intent_source": source,
    })


def _seed_all_kinds() -> None:
    _mk_run("20260801T130000Z-trade1", {
        "view.json": {"regime": "risk_on",
                      "candidates": [{"symbol": "TQQQ", "confidence": 0.7}]},
        "portfolio.json": {"positions": [{"symbol": "TQQQ"}], "all_cash": False,
                            "construction_rationale": "r"},
        "sanity.json": {"status": "pass", "summary": {"fail": 0}},
        "next_run.json": {"next_run_at": "2026-08-01T17:00:00Z"},
    })
    _decision("20260801T130000Z-trade1", "strategist", "trade")
    _mk_run("20260801T210000Z-review", {
        "signals.json": {},
        "review.json": {"regime": "trending_up",
                        "candidates": [{"symbol": "NUGT", "confidence": 0.0}]},
        "next_run.json": {"next_run_at": "2026-08-02T13:30:00Z"},
    })
    _decision("20260801T210000Z-review", "review_complete", "review")
    _mk_run("20260802T090000Z-closed", {
        "market_gate.json": {"is_open": False},
        "next_run.json": {"next_run_at": "2026-08-03T13:30:00Z",
                           "market_closed": True},
    })
    _mk_run("20260803T130000Z-dedup1", {
        "signals.json": {},
        "next_run.json": {"next_run_at": "2026-08-03T17:00:00Z",
                           "dedup_skipped": True},
    })
    _mk_run("20260803T170000Z-crash1", {
        "signals.json": {},
        "error.json": {"error_type": "RuntimeError"},
    })


def test_collect_labels_every_cycle_kind(tmp_state):
    _seed_all_kinds()
    rows = {r["run_id"]: r for r in analyze_runs.collect()}
    assert rows["20260801T130000Z-trade1"]["kind"] == "trade"
    assert rows["20260801T210000Z-review"]["kind"] == "review"
    assert rows["20260802T090000Z-closed"]["kind"] == "closed"
    assert rows["20260803T130000Z-dedup1"]["kind"] == "dedup"
    assert rows["20260803T170000Z-crash1"]["kind"] == "crash"


def test_review_rows_surface_regime_from_review_json(tmp_state):
    _seed_all_kinds()
    rows = {r["run_id"]: r for r in analyze_runs.collect()}
    review = rows["20260801T210000Z-review"]
    assert review["regime"] == "trending_up"
    assert review["candidates_count"] == 1
    assert review["cycle_intent"] == "review"


def test_review_artifact_fallback_without_decision_row(tmp_state):
    """A review run dir whose decision rows are gone (pruned log) still
    labels as review from the artifact alone."""
    _mk_run("20260805T210000Z-review", {
        "review.json": {"regime": "chop", "candidates": []},
        "next_run.json": {},
    })
    row = analyze_runs.collect()[0]
    assert row["cycle_intent"] == "review"
    assert row["kind"] == "review"


def test_markdown_renders_intent_and_legit_zeros(tmp_state, capsys):
    _seed_all_kinds()
    rows = analyze_runs.collect()
    analyze_runs._print_markdown(rows)
    out = capsys.readouterr().out
    assert "| intent |" in out
    assert "| crash |" in out
    assert "| closed |" in out
    # cost 0.0 and confidence 0.0 render as numbers, not "—".
    assert "| 0.0000 |" in out
    assert "| 0.00 |" in out
