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


# ---------- NAV display anchor ----------


def test_nav_offset_unset_returns_zero(tmp_state):
    """Fresh installs have no anchor — offset must be a no-op."""
    assert state.read_nav_offset() is None
    assert state.nav_offset_usd() == 0.0


def test_nav_offset_round_trip(tmp_state):
    """Set / read / clear cycle for the NAV anchor."""
    at = state.set_nav_offset(
        broker_baseline_usd=100020.52,
        virtual_baseline_usd=2500.0,
        note="test",
    )
    data = state.read_nav_offset()
    assert data is not None
    assert data["broker_baseline_usd"] == pytest.approx(100020.52)
    assert data["virtual_baseline_usd"] == pytest.approx(2500.0)
    assert data["set_at"] == at
    assert data["note"] == "test"
    # Offset = baseline − virtual = the dollar amount to subtract from
    # broker equity at render time.
    assert state.nav_offset_usd() == pytest.approx(97520.52)

    state.clear_nav_offset()
    assert state.read_nav_offset() is None
    assert state.nav_offset_usd() == 0.0


def test_nav_offset_corrupt_file_returns_none(tmp_state):
    """A malformed file must not crash the dashboard. Treat it as
    'no anchor' and let the operator re-anchor."""
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.NAV_OFFSET_FLAG.write_text("{not json", encoding="utf-8")
    assert state.read_nav_offset() is None
    assert state.nav_offset_usd() == 0.0


def test_nav_offset_missing_keys_returns_none(tmp_state):
    """File present but missing required fields → treat as no anchor."""
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.NAV_OFFSET_FLAG.write_text(
        '{"broker_baseline_usd": 100000}',  # missing virtual_baseline_usd
        encoding="utf-8",
    )
    assert state.read_nav_offset() is None
    assert state.nav_offset_usd() == 0.0


def test_nav_offset_default_virtual_baseline_is_2500(tmp_state):
    """CLAUDE.md spec target is $2,500 — make that the helper default."""
    state.set_nav_offset(broker_baseline_usd=100000.0)
    data = state.read_nav_offset()
    assert data["virtual_baseline_usd"] == pytest.approx(2500.0)


def test_manual_nav_baseline_unset_returns_none(tmp_state):
    """Fresh installs have no remembered manual baseline — the helper
    must be a no-op so dashboard inputs fall back to their hardcoded
    default."""
    assert state.read_manual_nav_baseline_usd() is None


def test_manual_nav_baseline_round_trip(tmp_state):
    """Set / read / clear cycle for the remembered manual baseline.
    Operator-flow: they enter $99,938.95 once, the dashboard remembers
    it across re-anchors and clears."""
    at = state.set_manual_nav_baseline_usd(99938.95)
    assert state.read_manual_nav_baseline_usd() == pytest.approx(99938.95)
    # File on disk carries the stamp too — useful when the operator
    # needs to audit when the manual baseline was set.
    import json
    data = json.loads(state.NAV_MANUAL_BASELINE_FLAG.read_text())
    assert data["set_at"] == at

    state.clear_manual_nav_baseline()
    assert state.read_manual_nav_baseline_usd() is None


def test_manual_nav_baseline_corrupt_file_returns_none(tmp_state):
    """Malformed file → fall back to None rather than crashing the
    Settings tab."""
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.NAV_MANUAL_BASELINE_FLAG.write_text("{not json", encoding="utf-8")
    assert state.read_manual_nav_baseline_usd() is None


def test_manual_nav_baseline_survives_anchor_changes(tmp_state):
    """The whole point of the remembered baseline: re-anchor /
    clear-anchor flows don't wipe it. Verifies the two state files
    are decoupled."""
    state.set_manual_nav_baseline_usd(99938.95)
    state.set_nav_offset(broker_baseline_usd=100020.0)  # auto-anchor
    assert state.read_manual_nav_baseline_usd() == pytest.approx(99938.95)
    state.clear_nav_offset()
    assert state.read_manual_nav_baseline_usd() == pytest.approx(99938.95)


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


# --------- mode tagging + live-transition marker (paper→live continuity) ---------


def _trade_row(**over):
    row = {
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 3, "fill_price": 70.0,
        "fees_usd": 0.0, "filled_at": "2026-07-01T14:00:00Z", "run_id": "r1",
    }
    row.update(over)
    return row


def test_append_trade_default_stamps_paper_mode(tmp_state):
    state.append_trade(_trade_row())
    row = state.read_trades()[0]
    assert row["mode"] == "paper"


def test_append_trade_preserves_explicit_live_mode(tmp_state):
    state.append_trade(_trade_row(mode="live"))
    row = state.read_trades()[0]
    assert row["mode"] == "live"


def test_append_nav_and_kill_event_default_paper_mode(tmp_state):
    state.append_nav({"run_id": "r1", "at": "2026-07-01T14:00:00Z", "nav_usd": 2500.0})
    state.append_kill_event({"at": "2026-07-01T14:00:00Z", "symbol": "TQQQ",
                             "reason": "loss cap"})
    nav = json.loads(state.NAV_HISTORY_LOG.read_text().splitlines()[0])
    kill = state.read_kill_events()[0]
    assert nav["mode"] == "paper"
    assert kill["mode"] == "paper"


def test_record_mode_missing_key_reads_as_paper():
    assert state.record_mode({}) == "paper"
    assert state.record_mode({"mode": None}) == "paper"
    assert state.record_mode({"mode": "live"}) == "live"


def test_trade_activity_dedup_ignores_mode_key(tmp_state):
    state.append_trade(_trade_row(activity_id="a1", mode="paper"))
    state.append_trade(_trade_row(activity_id="a2", mode="live"))
    assert state.read_trade_activity_ids() == {"a1", "a2"}


def test_write_live_transition_once_is_write_once(tmp_state):
    first = state.write_live_transition_once(
        live_starting_equity_usd=2612.34, nav_cap_usd=2500.0,
        run_id="r1", live_version=1,
    )
    assert first["live_starting_equity_usd"] == 2612.34
    assert first["nav_cap_usd"] == 2500.0
    again = state.write_live_transition_once(
        live_starting_equity_usd=9999.0, nav_cap_usd=None,
        run_id="r2", live_version=1,
    )
    assert again == first  # second write is a no-op
    assert state.read_live_transition() == first


def test_read_live_transition_missing_and_corrupt(tmp_state):
    assert state.read_live_transition() is None
    state.LIVE_TRANSITION.write_text("{not json", encoding="utf-8")
    assert state.read_live_transition() is None


def test_wipe_run_history_preserves_live_transition(tmp_state):
    """Codex P1 (PR #112): wiping history must NOT re-anchor the live risk
    budget — under a cap the marker anchors the P&L-since-transition debit,
    so a cleanup that removed it would silently restore a drawn-down
    allocation to the full cap. Re-anchoring is an explicit manual action."""
    state.write_live_transition_once(
        live_starting_equity_usd=2500.0, nav_cap_usd=None,
        run_id=None, live_version=1,
    )
    assert state.LIVE_TRANSITION.exists()
    state.wipe_run_history(backup=False)
    assert state.LIVE_TRANSITION.exists()


def test_dd_halt_is_mode_scoped(tmp_state):
    """Codex P2 (PR #112): a paper halt tripped earlier on promotion day is
    denominated in synthetic units and must not block live buys."""
    state.set_dd_halt(dd_pct=9.0, sod_nav=2500.0, current_nav=2270.0, mode="paper")
    assert state.dd_halt_active(mode="paper") is True
    assert state.dd_halt_active(mode="live") is False
    state.clear_dd_halt()
    state.set_dd_halt(dd_pct=9.0, sod_nav=5000.0, current_nav=4540.0, mode="live")
    assert state.dd_halt_active(mode="live") is True
    assert state.dd_halt_active(mode="paper") is False


def test_dd_halt_legacy_untagged_reads_as_paper(tmp_state):
    import json as _json
    state.DD_HALT_FLAG.write_text(_json.dumps({
        "date": state.utcnow().date().isoformat(),
        "dd_pct": 9.0, "sod_nav_usd": 2500.0, "current_nav_usd": 2270.0,
        "reason": "x", "set_at": state.utcnow_iso(),
    }), encoding="utf-8")
    assert state.dd_halt_active(mode="paper") is True
    assert state.dd_halt_active(mode="live") is False


def test_write_live_transition_once_atomic_create(tmp_state, monkeypatch):
    """Codex P2 (PR #112): two processes racing on the first live read must
    not both write — O_EXCL create means the loser reads the winner's
    marker. Simulate the race by making the pre-check miss an existing
    file that appears before our exclusive create."""
    winner = {"at": "2026-07-02T06:00:00Z", "live_starting_equity_usd": 5000.0,
              "nav_cap_usd": 2500.0, "run_id": "winner", "live_version": 1,
              "note": "first successful live equity read"}
    real_read = state.read_live_transition
    calls = {"n": 0}

    def racy_read():
        calls["n"] += 1
        if calls["n"] == 1:
            # First pre-check: pretend the file doesn't exist yet, then the
            # "winner" process creates it before our open("x").
            import json as _json
            state.LIVE_TRANSITION.write_text(_json.dumps(winner), encoding="utf-8")
            return None
        return real_read()

    monkeypatch.setattr(state, "read_live_transition", racy_read)
    out = state.write_live_transition_once(
        live_starting_equity_usd=4000.0, nav_cap_usd=2500.0,
        run_id="loser", live_version=1,
    )
    assert out["run_id"] == "winner"          # loser adopted the winner's marker
    assert out["live_starting_equity_usd"] == 5000.0


def test_write_live_transition_once_raises_on_corrupt_existing_file(tmp_state):
    """Codex P2 (PR #112): an existing-but-unreadable marker must surface as
    an error, not be silently replaced by a fresh in-memory marker that
    would re-anchor the capped live allocation to current equity."""
    state.LIVE_TRANSITION.write_text("{partial json", encoding="utf-8")
    with pytest.raises(OSError, match="unreadable"):
        state.write_live_transition_once(
            live_starting_equity_usd=5000.0, nav_cap_usd=2500.0,
            run_id="r1", live_version=1,
        )


# --------- user notes (dashboard "Note to agents" box) ---------


def test_user_note_append_read_pending_roundtrip(tmp_state):
    row = state.append_user_note("  wary of semis into NVDA earnings  ")
    assert row["text"] == "wary of semis into NVDA earnings"  # stripped
    assert row["id"] and row["at"]
    assert row["mode"] == "paper"
    notes = state.read_user_notes()
    assert len(notes) == 1 and notes[0]["id"] == row["id"]
    pending = state.pending_user_notes()
    assert [n["id"] for n in pending] == [row["id"]]


def test_user_note_rejects_empty_and_oversized(tmp_state):
    with pytest.raises(ValueError):
        state.append_user_note("   ")
    with pytest.raises(ValueError):
        state.append_user_note("x" * (state.USER_NOTE_MAX_CHARS + 1))
    assert state.read_user_notes() == []


def test_user_notes_missing_files_read_empty(tmp_state):
    assert state.read_user_notes() == []
    assert state.read_user_notes_consumed() == []
    assert state.read_consumed_note_ids() == set()
    assert state.pending_user_notes() == []


def test_user_notes_consumed_removes_from_pending_not_log(tmp_state):
    a = state.append_user_note("note a")
    b = state.append_user_note("note b")
    state.append_user_notes_consumed([a["id"]], run_id="r1")
    # Log keeps both rows — append-only, never mutated.
    assert len(state.read_user_notes()) == 2
    pending = state.pending_user_notes()
    assert [n["id"] for n in pending] == [b["id"]]
    markers = state.read_user_notes_consumed()
    assert markers[0]["note_id"] == a["id"]
    assert markers[0]["run_id"] == "r1"


def test_user_notes_age_window_expires_unconsumed(tmp_state):
    old = state.append_user_note("stale guidance")
    fresh = state.append_user_note("fresh guidance")
    # Rewrite the old row with a 15-day-old timestamp (test-only surgery on
    # the log file; production rows are always stamped at append time).
    rows = state.read_user_notes()
    rows[0]["at"] = "2026-06-20T00:00:00Z"
    state.USER_NOTES_LOG.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
    )
    from datetime import datetime, timezone
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    pending = state.pending_user_notes(now=now)
    assert [n["id"] for n in pending] == [fresh["id"]]
    assert old["id"] not in {n["id"] for n in pending}


def test_wipe_run_history_truncates_user_notes(tmp_state):
    state.append_user_note("note before wipe")
    state.append_user_notes_consumed(["x"], run_id="r1")
    state.wipe_run_history(backup=False)
    assert state.read_user_notes() == []
    assert state.read_user_notes_consumed() == []
