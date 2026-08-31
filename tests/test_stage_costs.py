"""Decision rows carry real per-stage cost + cache hit (costs.jsonl delta).

Historically ``_run_stage`` hard-coded ``prompt_cache_hit_pct: 0.0`` and
``cost_usd: 0.0`` on every row because it never saw the LLM call result.
It now snapshots ``state.read_costs_for_run`` before the runner and sums
the rows the runner appended (lib/llm.py records each call, retries
included, synchronously).
"""
from __future__ import annotations

import json

import orchestrator
from lib import state


def _cost_row(rid, stage, cost, inp, creation, read):
    return {
        "run_id": rid, "stage": stage, "model": "m", "cost_usd": cost,
        "at": state.utcnow_iso(), "input_tokens": inp, "output_tokens": 50,
        "cache_creation_input_tokens": creation, "cache_read_input_tokens": read,
    }


def _run(ctx, stage_id, runner):
    return orchestrator._run_stage(
        ctx=ctx, stage_id=stage_id, schema="", output_filename=f"{stage_id}.json",
        runner=runner, inputs_hash_parts=(stage_id,), model="m",
    )


def _rows():
    return [
        json.loads(line)
        for line in state.DECISIONS_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_run_stage_records_real_cost_and_cache_hit(tmp_state):
    ctx = orchestrator.StageContext(run_id="rid1", dry_run=True, broker=None)

    def runner():
        state.append_cost(_cost_row("rid1", "strategist", 0.5, 100, 0, 900))
        return {"ok": True}

    _run(ctx, "strategist", runner)
    row = _rows()[0]
    assert row["cost_usd"] == 0.5
    assert row["prompt_cache_hit_pct"] == 90.0


def test_run_stage_sums_retry_rows_and_scopes_to_own_delta(tmp_state):
    """A schema retry appends two cost rows in one stage — summed. A
    later stage sees only its own delta, not the earlier stage's."""
    ctx = orchestrator.StageContext(run_id="rid2", dry_run=True, broker=None)

    def first():
        state.append_cost(_cost_row("rid2", "construct", 0.2, 1000, 1000, 0))
        state.append_cost(_cost_row("rid2", "construct", 0.2, 500, 0, 1500))
        return {"ok": True}

    def second():
        state.append_cost(_cost_row("rid2", "critic", 0.03, 300, 100, 0))
        return {"ok": True}

    _run(ctx, "construct", first)
    _run(ctx, "critic", second)
    rows = _rows()
    construct = next(r for r in rows if r["stage"] == "construct")
    critic = next(r for r in rows if r["stage"] == "critic")
    assert construct["cost_usd"] == 0.4
    # (0 + 1500) reads / (1500 input + 1000 creation + 1500 reads) = 37.5%
    assert construct["prompt_cache_hit_pct"] == 37.5
    assert critic["cost_usd"] == 0.03
    assert critic["prompt_cache_hit_pct"] == 0.0


def test_run_stage_deterministic_stage_stays_zero(tmp_state):
    """No cost rows appended (deterministic / dry-run stage) → exactly the
    historical 0.0/0.0 — dry-run decision logs are unchanged."""
    ctx = orchestrator.StageContext(run_id="rid3", dry_run=True, broker=None)
    _run(ctx, "signals", lambda: {"ok": True})
    row = _rows()[0]
    assert row["cost_usd"] == 0.0
    assert row["prompt_cache_hit_pct"] == 0.0


def test_run_stage_cost_read_failure_degrades_not_raises(tmp_state, monkeypatch):
    ctx = orchestrator.StageContext(run_id="rid4", dry_run=True, broker=None)
    monkeypatch.setattr(
        state, "read_costs_for_run",
        lambda rid: (_ for _ in ()).throw(OSError("bad ledger")),
    )
    _run(ctx, "strategist", lambda: {"ok": True})
    row = _rows()[0]
    assert row["status"] == "ok"
    assert row["cost_usd"] == 0.0
    assert row["prompt_cache_hit_pct"] == 0.0
