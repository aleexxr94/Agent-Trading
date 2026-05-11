from __future__ import annotations

import json

import pytest
from jsonschema import ValidationError

from lib import state


def test_run_id_format(tmp_state):
    rid = state.new_run_id()
    assert "T" in rid and rid.endswith(tuple("0123456789abcdef"))
    assert len(rid.split("-")[-1]) == 6


def test_halt_flag_round_trip(tmp_state):
    assert not state.is_halted()
    state.set_halt("test")
    assert state.is_halted()
    state.clear_halt()
    assert not state.is_halted()


def test_atomic_write_json(tmp_state):
    p = tmp_state / "out.json"
    state.write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    # No tempfiles left behind
    leftovers = [x for x in p.parent.iterdir() if x.name.startswith("out.json.")]
    assert leftovers == []


def test_write_json_validates_against_schema(tmp_state):
    bad_decision = {"run_id": "x", "stage": "magic"}
    with pytest.raises(ValidationError):
        state.write_json(tmp_state / "d.json", bad_decision, schema="decision_log.schema.json")


def test_append_decision_validates(tmp_state):
    good = {
        "run_id": "rid",
        "stage": "screen",
        "model": "claude-haiku-4-5-20251001",
        "inputs_hash": "deadbeef" * 2,
        "output_ref": "screen.json",
        "prompt_cache_hit_pct": 80.0,
        "cost_usd": 0.04,
        "started_at": "2026-05-10T12:00:00Z",
        "ended_at": "2026-05-10T12:00:05Z",
        "status": "ok",
        "risk_warning": "PAPER TRADING.",
    }
    state.append_decision(good)
    state.append_decision({**good, "stage": "research"})
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert len(lines) == 2

    with pytest.raises(ValidationError):
        state.append_decision({**good, "stage": "magic"})


def test_append_cost_filters_today_and_run(tmp_state, monkeypatch):
    today_iso = state.utcnow_iso()
    state.append_cost({"run_id": "r1", "stage": "screen", "model": "m", "cost_usd": 0.01, "at": today_iso})
    state.append_cost({"run_id": "r2", "stage": "screen", "model": "m", "cost_usd": 0.02, "at": today_iso})
    state.append_cost({"run_id": "r1", "stage": "research", "model": "m", "cost_usd": 0.03, "at": "2020-01-01T00:00:00Z"})

    today_rows = state.read_costs_today()
    assert len(today_rows) == 2
    r1_rows = state.read_costs_for_run("r1")
    assert sum(r["cost_usd"] for r in r1_rows) == pytest.approx(0.04)


def test_append_cost_rejects_missing_keys(tmp_state):
    with pytest.raises(ValueError):
        state.append_cost({"run_id": "r1"})


def test_append_and_read_nav_history(tmp_state):
    assert state.read_nav_history() == []
    state.append_nav({
        "run_id": "r1", "at": state.utcnow_iso(), "nav_usd": 2500.0,
        "cash_usd": 280.0, "positions_count": 9, "all_cash": False,
        "gross_pnl_usd": 0.0, "modelled_costs_usd": 4.55, "net_pnl_usd": -4.55,
    })
    state.append_nav({
        "run_id": "r2", "at": state.utcnow_iso(), "nav_usd": 2540.0,
        "positions_count": 9, "all_cash": False,
        "gross_pnl_usd": 50.0, "modelled_costs_usd": 4.55, "net_pnl_usd": 45.45,
    })
    hist = state.read_nav_history()
    assert len(hist) == 2
    assert hist[0]["run_id"] == "r1"
    assert hist[1]["nav_usd"] == 2540.0


def test_read_nav_history_limit(tmp_state):
    for i in range(5):
        state.append_nav({"run_id": f"r{i}", "at": state.utcnow_iso(), "nav_usd": 2500.0 + i})
    assert len(state.read_nav_history()) == 5
    assert len(state.read_nav_history(limit=2)) == 2
    assert state.read_nav_history(limit=2)[-1]["run_id"] == "r4"


def test_append_nav_rejects_missing_keys(tmp_state):
    with pytest.raises(ValueError):
        state.append_nav({"run_id": "r1", "at": state.utcnow_iso()})  # missing nav_usd
