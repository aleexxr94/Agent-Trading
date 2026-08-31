"""Pipeline crash handler — unhandled exceptions must leave a record and
a fresh next_run.json instead of a silent stall.

Before this handler existed, any raise inside run_pipeline unwound through
main() to the interpreter: no decision row, no error.json, and a STALE
state/next_run.json that pinned deploy/run_scheduler.sh (LAST_FIRED ==
NEXT_AT) until the daily 13:30 UTC fallback timer. The §Promotion
"unresolved failures" gate counted nothing because nothing was written.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import orchestrator
from lib import llm, state


def _boom(exc: Exception):
    def _runner(**kwargs):
        raise exc
    return _runner


def _run_main(monkeypatch, exc: Exception, argv: list[str] | None = None):
    """Invoke orchestrator.main with run_pipeline raising `exc`."""
    monkeypatch.setattr(orchestrator, "run_pipeline", _boom(exc))
    monkeypatch.setattr(orchestrator, "_try_load_broker", lambda: None)
    return orchestrator.main(argv if argv is not None else [])


def _decisions() -> list[dict]:
    if not state.DECISIONS_LOG.exists():
        return []
    return [
        json.loads(line)
        for line in state.DECISIONS_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _minutes_from_now(iso: str) -> float:
    at = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (at - datetime.now(timezone.utc)).total_seconds() / 60.0


def test_generic_crash_writes_row_artifact_and_reschedule(tmp_state, monkeypatch):
    with pytest.raises(RuntimeError, match="boom"):
        _run_main(monkeypatch, RuntimeError("boom"), ["--run-id", "crash1"])

    rows = _decisions()
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "pipeline"
    assert row["status"] == "error"
    assert row["error"] == "RuntimeError: boom"
    assert row["output_ref"] == "error.json"

    err = json.loads((state.run_dir("crash1") / "error.json").read_text())
    assert err["error_type"] == "RuntimeError"
    assert "Traceback" in err["traceback"]

    # Fresh reschedule ~CRASH_RETRY_MINUTES out, in both the run dir and
    # state/next_run.json (non-dry-run), so the scheduler un-pins.
    nr = json.loads(state.NEXT_RUN.read_text())
    assert nr["run_id"] == "crash1"
    assert nr["crash"] is True
    assert 0 < _minutes_from_now(nr["next_run_at"]) <= orchestrator.CRASH_RETRY_MINUTES + 1
    run_nr = json.loads((state.run_dir("crash1") / "next_run.json").read_text())
    assert run_nr == nr


def test_dry_run_crash_never_touches_state_next_run(tmp_state, monkeypatch):
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, RuntimeError("boom"), ["--dry-run", "--run-id", "crashdry"])
    assert not state.NEXT_RUN.exists()
    # ... but the run dir still records what happened.
    assert (state.run_dir("crashdry") / "error.json").exists()
    assert (state.run_dir("crashdry") / "next_run.json").exists()
    assert _decisions()[0]["status"] == "error"


def test_halt_flag_exception_is_not_a_crash(tmp_state, monkeypatch):
    """Operator halt propagates untouched: no row, no artifact, no reschedule."""
    with pytest.raises(llm.HaltFlagSet):
        _run_main(monkeypatch, llm.HaltFlagSet("halted"), ["--run-id", "h1"])
    assert _decisions() == []
    assert not (state.run_dir("h1") / "error.json").exists()
    assert not state.NEXT_RUN.exists()


def test_halt_set_mid_run_records_but_does_not_reschedule(tmp_state, monkeypatch):
    """A crash while the halt flag is up still gets recorded, but halt
    means stop — no fresh next_run.json."""
    state.HALT_FLAG.touch()
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, RuntimeError("boom"), ["--run-id", "h2"])
    assert _decisions()[0]["status"] == "error"
    assert (state.run_dir("h2") / "error.json").exists()
    assert not state.NEXT_RUN.exists()


def test_daily_cost_cap_aborts_until_next_utc_day(tmp_state, monkeypatch):
    with pytest.raises(llm.CostCapExceeded):
        _run_main(
            monkeypatch,
            llm.CostCapExceeded("daily cap $12.00 reached", cap="daily"),
            ["--run-id", "cap1"],
        )
    row = _decisions()[0]
    assert row["status"] == "aborted"  # designed guardrail, not a gate failure
    nr = json.loads(state.NEXT_RUN.read_text())
    at = datetime.strptime(nr["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert at.date() > now.date()
    assert (at.hour, at.minute) == (0, 5)


def test_per_run_cost_cap_retries_in_four_hours(tmp_state, monkeypatch):
    with pytest.raises(llm.CostCapExceeded):
        _run_main(
            monkeypatch,
            llm.CostCapExceeded("per-run cap $3.00 reached", cap="per_run"),
            ["--run-id", "cap2"],
        )
    assert _decisions()[0]["status"] == "aborted"
    nr = json.loads(state.NEXT_RUN.read_text())
    assert 235 <= _minutes_from_now(nr["next_run_at"]) <= 241


def test_existing_schedule_from_same_run_is_not_overridden(tmp_state, monkeypatch):
    """Crash after stage_execute already wrote NEXT_RUN (meta's pick):
    keep the meta-scheduler's choice."""
    meta_pick = {"run_id": "late1", "next_run_at": "2099-01-01T00:00:00Z"}
    state.write_json(state.NEXT_RUN, meta_pick)
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, RuntimeError("post-execute boom"), ["--run-id", "late1"])
    assert json.loads(state.NEXT_RUN.read_text()) == meta_pick
    assert _decisions()[0]["status"] == "error"  # still recorded


def test_stale_schedule_from_prior_run_is_replaced(tmp_state, monkeypatch):
    stale = {"run_id": "someoldrun", "next_run_at": "2020-01-01T00:00:00Z"}
    state.write_json(state.NEXT_RUN, stale)
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, RuntimeError("boom"), ["--run-id", "fresh1"])
    nr = json.loads(state.NEXT_RUN.read_text())
    assert nr["run_id"] == "fresh1"
    assert nr["next_run_at"] != "2020-01-01T00:00:00Z"


def test_keyboard_interrupt_propagates_untouched(tmp_state, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_pipeline", _boom(KeyboardInterrupt()))
    monkeypatch.setattr(orchestrator, "_try_load_broker", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.main(["--run-id", "ki1"])
    assert _decisions() == []
    assert not (state.run_dir("ki1") / "error.json").exists()


def test_broken_decision_log_never_masks_the_original_error(tmp_state, monkeypatch):
    """Each bookkeeping step is best-effort: if append_decision itself
    raises, the original exception still propagates and the other
    artifacts are still written."""
    monkeypatch.setattr(
        state, "append_decision",
        lambda entry: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        _run_main(monkeypatch, RuntimeError("boom"), ["--run-id", "b0rk"])
    assert (state.run_dir("b0rk") / "error.json").exists()
    assert state.NEXT_RUN.exists()
