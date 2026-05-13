from __future__ import annotations

import json
from pathlib import Path

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


# ---------- wipe_run_history (Settings tab "start fresh" button) ----------


def test_wipe_run_history_clears_jsonl_logs(tmp_state):
    """Append to all three JSONL audit logs then wipe — expect every
    file present-but-empty (truncated, not unlinked)."""
    state.append_decision({
        "run_id": "rid1", "stage": "signals", "model": "x",
        "inputs_hash": "deadbeefcafebabe1234", "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": "2026-05-13T14:00:00Z",
        "ended_at": "2026-05-13T14:00:01Z",
        "status": "ok", "risk_warning": "test",
    })
    state.append_cost({"run_id": "rid1", "stage": "construct",
                       "model": "opus", "cost_usd": 0.20,
                       "at": "2026-05-13T14:01:00Z"})
    state.append_nav({"run_id": "rid1", "at": "2026-05-13T14:02:00Z",
                      "nav_usd": 2500.0, "positions_count": 0,
                      "all_cash": True, "gross_pnl_usd": 0.0,
                      "modelled_costs_usd": 0.0, "net_pnl_usd": 0.0})

    result = state.wipe_run_history(include_costs=True, backup=False)

    # All three JSONLs truncated (still exist, but empty)
    assert state.DECISIONS_LOG.exists()
    assert state.DECISIONS_LOG.read_text() == ""
    assert state.COSTS_LOG.read_text() == ""
    assert state.NAV_HISTORY_LOG.read_text() == ""

    # Summary dict reflects what was cleared
    assert set(result["jsonl_truncated"]) >= {
        "decisions.jsonl", "costs.jsonl", "nav_history.jsonl",
    }


def test_wipe_run_history_preserves_costs_when_include_costs_false(tmp_state):
    """include_costs=False keeps the cost audit log intact — caps
    continue to enforce against historical spend."""
    state.append_cost({"run_id": "r", "stage": "x", "model": "m",
                       "cost_usd": 1.50, "at": "2026-05-13T14:00:00Z"})
    state.wipe_run_history(include_costs=False, backup=False)
    assert state.COSTS_LOG.exists()
    assert state.COSTS_LOG.read_text().strip()  # still has the row


def test_wipe_run_history_preserves_halt_flag(tmp_state):
    """The halt flag represents operator stop-intent; this button must
    not override it."""
    state.set_halt("test")
    assert state.HALT_FLAG.exists()
    state.wipe_run_history(backup=False)
    assert state.HALT_FLAG.exists(), "halt.flag must survive a history wipe"


def test_wipe_run_history_removes_runs_and_snapshots(tmp_state):
    """state/runs/* + snapshot files (current_portfolio, next_run,
    last_cycle_hash) all go away."""
    rid = state.new_run_id()
    state.run_dir(rid).mkdir(parents=True, exist_ok=True)
    state.write_json(state.run_dir(rid) / "signals.json", {"tickers": []})
    state.write_json(state.CURRENT_PORTFOLIO, {"positions": []})
    state.write_json(state.NEXT_RUN, {"next_run_at": "2026-05-13T18:00:00Z"})
    state.write_json(state.LAST_CYCLE_HASH, {"signals_fingerprint": "x"})

    result = state.wipe_run_history(backup=False)

    # Run dirs gone
    assert list(state.RUNS_DIR.iterdir()) == []
    assert result["runs_dirs_removed"] >= 1

    # Snapshots gone
    assert not state.CURRENT_PORTFOLIO.exists()
    assert not state.NEXT_RUN.exists()
    assert not state.LAST_CYCLE_HASH.exists()
    assert set(result["snapshots_removed"]) >= {
        "current_portfolio.json", "next_run.json", "last_cycle_hash.json",
    }


def test_wipe_run_history_writes_backup_when_requested(tmp_state):
    """backup=True (default) copies state files to a timestamped backup
    dir before deleting. Operator can restore from there if needed."""
    state.append_decision({
        "run_id": "rid", "stage": "signals", "model": "x",
        "inputs_hash": "deadbeefcafebabe1234", "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": "2026-05-13T14:00:00Z",
        "ended_at": "2026-05-13T14:00:01Z",
        "status": "ok", "risk_warning": "t",
    })

    result = state.wipe_run_history(include_costs=True, backup=True)

    assert result["backup_dir"] is not None
    backup_dir = Path(result["backup_dir"])
    assert backup_dir.exists()
    assert backup_dir.is_dir()
    assert backup_dir.name.startswith("backup_")
    # The pre-wipe decisions.jsonl is in the backup
    bd = backup_dir / "decisions.jsonl"
    assert bd.exists()
    assert "signals" in bd.read_text()


def test_wipe_run_history_succeeds_when_state_is_already_empty(tmp_state):
    """Calling wipe on a clean state/ directory must not error.
    Returns a summary showing 0 of everything."""
    result = state.wipe_run_history(backup=False)
    assert result["runs_dirs_removed"] == 0
    assert result["jsonl_truncated"] == []
    assert result["snapshots_removed"] == []


def test_wipe_run_history_backup_dir_collision_safe(tmp_state, monkeypatch):
    """Codex P2 on PR #70: when two wipes land in the same microsecond
    (rapid double-click, automation), the second backup must NOT
    silently fail and let the wipe proceed without a safety net.

    Simulate by freezing utcnow() so both calls produce the same
    timestamp. The retry-with-suffix loop should give the second
    backup dir an alternate name and still succeed.
    """
    from datetime import datetime, timezone
    frozen = datetime(2026, 5, 13, 22, 0, 0, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(state, "utcnow", lambda: frozen)

    state.append_decision({
        "run_id": "r1", "stage": "signals", "model": "x",
        "inputs_hash": "deadbeefcafebabe1234", "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": "2026-05-13T14:00:00Z",
        "ended_at": "2026-05-13T14:00:01Z",
        "status": "ok", "risk_warning": "t",
    })

    r1 = state.wipe_run_history(backup=True)
    state.append_decision({
        "run_id": "r2", "stage": "signals", "model": "x",
        "inputs_hash": "deadbeefcafebabe1234", "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": "2026-05-13T14:00:00Z",
        "ended_at": "2026-05-13T14:00:01Z",
        "status": "ok", "risk_warning": "t",
    })
    r2 = state.wipe_run_history(backup=True)

    # Both wipes must have created distinct backup directories.
    assert r1["backup_dir"] is not None, "first wipe should have backed up"
    assert r2["backup_dir"] is not None, (
        "second same-microsecond wipe must NOT silently skip backup"
    )
    assert r1["backup_dir"] != r2["backup_dir"], (
        "collision-safe naming must yield distinct paths"
    )
    assert Path(r1["backup_dir"]).exists()
    assert Path(r2["backup_dir"]).exists()


def test_wipe_run_history_idempotent(tmp_state):
    """Two consecutive wipes must both succeed (second is a no-op)."""
    state.append_decision({
        "run_id": "r", "stage": "signals", "model": "x",
        "inputs_hash": "deadbeefcafebabe1234", "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": "2026-05-13T14:00:00Z",
        "ended_at": "2026-05-13T14:00:01Z",
        "status": "ok", "risk_warning": "t",
    })
    r1 = state.wipe_run_history(backup=False)
    r2 = state.wipe_run_history(backup=False)
    assert len(r1["jsonl_truncated"]) >= 1
    # Second wipe truncates files that are already empty — still legal
    # (write_text("") is idempotent) — backup dir count is 0 since we
    # passed backup=False.
    assert r2["runs_dirs_removed"] == 0
