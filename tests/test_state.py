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


def test_cost_reset_round_trip(tmp_state):
    """Operator marks a reset → read_cost_reset_at returns the timestamp;
    clear removes it. Underlying behaviour for the dashboard's '🔄 Reset
    daily cost meter' button."""
    assert state.read_cost_reset_at() is None
    at = state.set_cost_reset("test")
    assert state.read_cost_reset_at() == at
    state.clear_cost_reset()
    assert state.read_cost_reset_at() is None


def test_read_costs_today_filters_by_reset_marker(tmp_state, monkeypatch):
    """When the reset marker is set, read_costs_today only returns rows
    timestamped AFTER it. Same-UTC-day check: a yesterday-reset shouldn't
    affect today's accounting."""
    today = state.utcnow().date().isoformat()
    # Rows: two before the reset, one after.
    state.append_cost({
        "run_id": "r1", "stage": "signals", "model": "x", "cost_usd": 0.10,
        "at": f"{today}T05:00:00Z",
    })
    state.append_cost({
        "run_id": "r2", "stage": "signals", "model": "x", "cost_usd": 0.20,
        "at": f"{today}T09:00:00Z",
    })
    # Plant a reset marker at 10:00 today
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.COST_RESET_FLAG.write_text(
        f'{{"at": "{today}T10:00:00Z", "reason": "test"}}',
        encoding="utf-8",
    )
    state.append_cost({
        "run_id": "r3", "stage": "signals", "model": "x", "cost_usd": 0.40,
        "at": f"{today}T11:00:00Z",
    })

    rows = state.read_costs_today()
    # Only the post-reset row should survive
    assert [r["run_id"] for r in rows] == ["r3"]
    assert sum(r["cost_usd"] for r in rows) == 0.40


def test_read_costs_today_ignores_yesterday_reset(tmp_state):
    """A reset marker from yesterday's UTC day must not filter today's rows."""
    today = state.utcnow().date().isoformat()
    state.append_cost({
        "run_id": "r1", "stage": "signals", "model": "x", "cost_usd": 0.10,
        "at": f"{today}T05:00:00Z",
    })
    # Plant a yesterday-dated reset marker
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T23:59:59Z", "reason": "old"}',
        encoding="utf-8",
    )
    rows = state.read_costs_today()
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r1"


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
        "stage": "signals",
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
    state.append_decision({**good, "stage": "strategist"})
    lines = state.DECISIONS_LOG.read_text().strip().splitlines()
    assert len(lines) == 2

    with pytest.raises(ValidationError):
        state.append_decision({**good, "stage": "magic"})


def test_append_cost_filters_today_and_run(tmp_state, monkeypatch):
    today_iso = state.utcnow_iso()
    state.append_cost({"run_id": "r1", "stage": "signals", "model": "m", "cost_usd": 0.01, "at": today_iso})
    state.append_cost({"run_id": "r2", "stage": "signals", "model": "m", "cost_usd": 0.02, "at": today_iso})
    state.append_cost({"run_id": "r1", "stage": "strategist", "model": "m", "cost_usd": 0.03, "at": "2020-01-01T00:00:00Z"})

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


# ---------- all-time cost reset ----------


def test_all_time_cost_reset_round_trip(tmp_state):
    """Set / read / clear cycle for the all-time reset marker."""
    assert state.read_all_time_cost_reset_at() is None
    at = state.set_all_time_cost_reset("test")
    assert state.read_all_time_cost_reset_at() == at
    state.clear_all_time_cost_reset()
    assert state.read_all_time_cost_reset_at() is None


def test_set_all_time_cost_reset_also_stamps_daily_marker(tmp_state):
    """`Reset ALL costs` should zero today's meter too — the operator asked
    for 'reset all costs up to date', not just historical totals.
    set_all_time_cost_reset writes BOTH markers to the same timestamp."""
    assert state.read_cost_reset_at() is None
    at = state.set_all_time_cost_reset("test")
    assert state.read_all_time_cost_reset_at() == at
    assert state.read_cost_reset_at() == at  # daily marker also stamped


def test_clear_all_time_cost_reset_also_clears_daily_marker(tmp_state):
    """Codex P2 (PR #53): clear must be symmetric with set. After
    set_all_time_cost_reset stamps BOTH markers, clear_all_time_cost_reset
    must drop BOTH — otherwise today's meter stays filtered and the
    dashboard ends up in a partially-reset state."""
    state.set_all_time_cost_reset("test")
    assert state.read_all_time_cost_reset_at() is not None
    assert state.read_cost_reset_at() is not None
    state.clear_all_time_cost_reset()
    assert state.read_all_time_cost_reset_at() is None
    assert state.read_cost_reset_at() is None, (
        "daily marker must also be cleared so 'show full history' "
        "actually shows full history including today"
    )


def test_filter_costs_post_reset_passthrough_when_no_marker(tmp_state):
    """No reset marker → filter is identity."""
    rows = [
        {"run_id": "r1", "at": "2026-05-10T05:00:00Z", "cost_usd": 0.1},
        {"run_id": "r2", "at": "2026-05-11T08:00:00Z", "cost_usd": 0.2},
    ]
    assert state.filter_costs_post_reset(rows) == rows


def test_filter_costs_post_reset_drops_at_or_before_marker(tmp_state):
    """Reset planted at 2026-05-12T10:00:00Z → rows ≤ that drop, rows after stay."""
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-12T10:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    rows = [
        {"run_id": "old1", "at": "2026-05-10T05:00:00Z", "cost_usd": 0.10},
        {"run_id": "old2", "at": "2026-05-12T09:59:59Z", "cost_usd": 0.20},
        {"run_id": "boundary", "at": "2026-05-12T10:00:00Z", "cost_usd": 0.30},  # ≤ marker
        {"run_id": "new", "at": "2026-05-12T10:00:01Z", "cost_usd": 0.40},
    ]
    out = state.filter_costs_post_reset(rows)
    assert [r["run_id"] for r in out] == ["new"]


def test_filter_costs_post_reset_does_not_mutate_input(tmp_state):
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-12T10:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    rows = [
        {"run_id": "old", "at": "2026-05-10T05:00:00Z", "cost_usd": 0.10},
        {"run_id": "new", "at": "2026-05-12T10:00:01Z", "cost_usd": 0.40},
    ]
    state.filter_costs_post_reset(rows)
    assert len(rows) == 2  # not mutated
