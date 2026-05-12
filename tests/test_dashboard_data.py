"""Pure-data-layer tests for the dashboard. No streamlit dependency required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import dashboard_data as dd
from lib import state


FIXTURE = Path(__file__).parent / "fixtures" / "portfolio.json"


def test_load_portfolio_falls_back_to_fixture(tmp_state):
    p, src = dd.load_portfolio()
    assert src == "fixture"
    assert 1 <= len(p["positions"]) <= 12


def test_load_portfolio_prefers_live(tmp_state):
    state.write_json(state.CURRENT_PORTFOLIO, json.loads(FIXTURE.read_text()))
    p, src = dd.load_portfolio()
    assert src == "live"


def test_load_portfolio_seed_overrides_fixture(tmp_state):
    seed = state.STATE_DIR / "seed_portfolio.json"
    state.write_json(seed, json.loads(FIXTURE.read_text()))
    _, src = dd.load_portfolio()
    assert src == "seed"


# ---------- mark_key_for_position ----------


def test_mark_key_for_position_etf():
    assert dd.mark_key_for_position({"kind": "etf", "symbol": "TQQQ"}) == "TQQQ"


def test_mark_key_for_position_option():
    pos = {
        "kind": "option", "underlying": "SPY", "strike": 530.0,
        "expiry": "2026-06-19", "type": "call",
    }
    assert dd.mark_key_for_position(pos) == "SPY|530.0|2026-06-19|call"


# ---------- split_positions_by_broker_holdings ----------


def test_split_returns_all_open_when_held_keys_none():
    """held_keys=None signals broker unreachable; don't filter anything."""
    portfolio = json.loads(FIXTURE.read_text())
    open_, closed = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=None,
    )
    assert len(open_) == len(portfolio["positions"])
    assert closed == []


def test_split_treats_empty_held_keys_as_all_closed():
    """held_keys=set() = broker reachable, says zero positions → every
    portfolio entry is treated as closed."""
    portfolio = json.loads(FIXTURE.read_text())
    open_, closed = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=frozenset(),
    )
    assert open_ == []
    assert len(closed) == len(portfolio["positions"])


def test_split_partitions_etf_and_option_correctly():
    """Mixed portfolio: TQQQ + SPY 530C still open at broker; rest closed."""
    portfolio = json.loads(FIXTURE.read_text())
    held = frozenset({"TQQQ", "SPY|530.0|2026-06-19|call"})
    open_, closed = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=held,
    )
    assert len(open_) == 2
    assert {dd.mark_key_for_position(p) for p in open_} == set(held)
    # Everything else closed.
    assert len(closed) == len(portfolio["positions"]) - 2


# ---------- position_table_rows held_keys filter ----------


def test_position_table_rows_filters_stale_positions_when_broker_reachable(tmp_state):
    """Regression for May 12 2026: dashboard kept rendering a position
    after it closed on Alpaca because portfolio.json still listed it.
    Now: when held_keys is supplied, only those keys render."""
    portfolio = json.loads(FIXTURE.read_text())
    # Broker holds only TQQQ; everything else got manually closed.
    held = frozenset({"TQQQ"})
    rows = dd.position_table_rows(portfolio, held_keys=held)
    assert len(rows) == 1
    assert rows[0]["Symbol"] == "TQQQ"


def test_position_table_rows_renders_everything_when_held_keys_none(tmp_state):
    """held_keys=None = broker unreachable; don't blank the dashboard."""
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio, held_keys=None)
    assert len(rows) == len(portfolio["positions"])


def test_position_table_rows_empty_when_held_keys_empty(tmp_state):
    """held_keys=set() = broker reachable, zero positions → table empty."""
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio, held_keys=frozenset())
    assert rows == []


# ---------- BrokerView ----------


def test_broker_view_dataclass_shape():
    """BrokerView's held_keys must equal set(costs) so the dashboard's
    filter is consistent with the cost dict used for P&L."""
    view = dd.BrokerView(
        marks={"TQQQ": 80.0},
        costs={"TQQQ": 75.0, "SPY|530.0|2026-06-19|call": 8.10},
        held_keys=frozenset({"TQQQ", "SPY|530.0|2026-06-19|call"}),
        available=True,
    )
    assert set(view.costs) == set(view.held_keys)
    assert view.available is True


def test_try_load_broker_view_sets_available_false_when_get_positions_raises(
    tmp_state, monkeypatch
):
    """Codex P1 (PR #51 review): marks_from_broker and
    cost_basis_from_broker both swallow get_positions() exceptions and
    return {}, which is indistinguishable from a broker that legitimately
    holds zero positions. try_load_broker_view must call get_positions()
    itself so transient broker failures flip available=False — otherwise
    a flaky network blanks the entire dashboard table by treating every
    portfolio.json entry as 'closed at broker'."""

    class _FailingBroker:
        def get_positions(self):
            raise RuntimeError("simulated alpaca 500")

    import lib.alpaca_client as ac_mod
    monkeypatch.setattr(ac_mod, "AlpacaBroker", lambda *a, **kw: _FailingBroker())

    view = dd.try_load_broker_view()
    assert view.available is False, (
        "broker call failed transiently — must report unavailable, not "
        "available with empty held_keys (which would blank the dashboard)"
    )
    assert view.marks == {}
    assert view.costs == {}
    assert view.held_keys == frozenset()


def test_try_load_broker_view_distinguishes_unreachable_from_empty(
    tmp_state, monkeypatch
):
    """Sister regression: when the broker is reachable and reports an
    empty position list (a genuine all-cash account), available=True and
    held_keys=set() so the dashboard correctly hides stale portfolio.json
    rows."""

    class _EmptyBroker:
        def get_positions(self):
            return []

    import lib.alpaca_client as ac_mod
    monkeypatch.setattr(ac_mod, "AlpacaBroker", lambda *a, **kw: _EmptyBroker())

    view = dd.try_load_broker_view()
    assert view.available is True
    assert view.held_keys == frozenset()


def test_position_table_rows_etf_and_option_columns(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio)
    kinds = {r["Kind"] for r in rows}
    assert kinds == {"ETF", "OPT"}
    opt_row = next(r for r in rows if r["Kind"] == "OPT")
    assert "Δ" in opt_row["Greeks"]
    assert opt_row["Kill"] == "≤100%"


def test_position_table_rows_without_marks_leaves_pnl_blank(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio)
    for r in rows:
        assert r["Mark"] is None
        assert r["Gross P&L"] is None
        assert r["Net P&L"] is None


def test_position_table_rows_etf_pnl_with_marks(tmp_state):
    """ETF mark above cost → positive gross; net subtracts modelled costs."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio, marks={"TQQQ": 80.0})
    r = rows[0]
    assert r["Mark"] == 80.0
    # 4 shares × (80 - 70) = $40 gross
    assert r["Gross P&L"] == pytest.approx(40.0)
    # Net is below gross by some modelled round-trip cost.
    assert r["Net P&L"] < r["Gross P&L"]


def _option_pos():
    return {
        "kind": "option", "underlying": "SPY", "type": "call",
        "strike": 530.0, "expiry": "2026-06-19", "dte": 40,
        "contracts": 1, "premium_paid": 6.50,
        "greeks": {"delta": 0.45, "gamma": 0.02, "theta": -0.04, "vega": 0.18,
                    "iv": 0.18, "iv_percentile": 35},
        "entry_thesis": "x", "kill_conditions": {"max_loss_pct": 100},
        "position_pct": 5.0,
    }


def test_position_table_rows_option_pnl_uses_synthetic_key(tmp_state):
    """Option marks use 'underlying|strike|expiry|type' key, same as monitor.py."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},
        marks={"SPY|530.0|2026-06-19|call": 8.00},
    )
    r = rows[0]
    assert r["Mark"] == 8.00
    # 1 contract × 100 × (8.00 - 6.50) = $150 gross
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_position_table_rows_option_pnl_falls_back_to_osi_key(tmp_state):
    """marks_from_broker currently keys options by OSI symbol. Until that
    aligns with the synthetic key, the dashboard must accept either so live
    option marks actually flow into the Mark / Gross / Net columns.

    Regression test for the P1 Codex flagged on PR #21: without this fallback,
    option P&L stays blank in any portfolio holding options.
    """
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},
        marks={"SPY260619C00530000": 8.00},
    )
    r = rows[0]
    assert r["Mark"] == 8.00
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_allocation_pie_includes_cash(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    pie = dd.allocation_pie(portfolio)
    assert any(r["label"] == "Cash" for r in pie)
    assert sum(r["value"] for r in pie) == pytest.approx(100.0, abs=0.5)


def test_position_pnl_uses_broker_cost_basis_when_provided(tmp_state):
    """Regression for the live observation on May 12 2026:
       Agent's portfolio.json said premium_paid=3.50 (its training-data prior)
       Alpaca actually filled at avg_cost=0.61
       Dashboard used to show -$290 fictional loss when truth was -$2.
    The `costs` dict passed into position_table_rows must override
    portfolio.json's premium_paid when computing Cost / Notional / P&L."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},  # premium_paid=6.50 in fixture
        marks={"SPY|530.0|2026-06-19|call": 0.59},
        costs={"SPY|530.0|2026-06-19|call": 0.61},
    )
    r = rows[0]
    # Cost column shows broker's actual fill, not the agent's premium_paid
    assert r["Cost"] == pytest.approx(0.61)
    # Notional reflects truth: 1 contract × 100 × $0.61 = $61
    assert r["Notional"] == pytest.approx(61.0)
    # Gross P&L: ($0.59 - $0.61) × 1 × 100 = -$2  (NOT the -$590 we'd see
    # if we used the fixture's premium_paid=6.50 as basis)
    assert r["Gross P&L"] == pytest.approx(-2.0)


def test_position_pnl_falls_back_to_portfolio_premium_when_no_broker_costs(tmp_state):
    """When the broker is unreachable (no costs dict), keep the old
    behaviour and use portfolio.json's premium_paid. Otherwise we'd
    silently lose P&L for offline / no-keys configurations."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},  # premium_paid=6.50
        marks={"SPY|530.0|2026-06-19|call": 8.00},
        costs=None,  # broker unavailable
    )
    r = rows[0]
    assert r["Cost"] == pytest.approx(6.50)
    # ($8.00 - $6.50) × 1 × 100 = $150
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_position_pnl_etf_uses_broker_cost_basis_when_provided(tmp_state):
    """Same broker-truth principle for ETFs: prefer the broker's filled
    avg_cost over the portfolio's intended avg_cost."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(
        portfolio,
        marks={"TQQQ": 80.0},
        costs={"TQQQ": 75.0},  # agent thought $70, actually filled at $75
    )
    r = rows[0]
    assert r["Cost"] == pytest.approx(75.0)
    assert r["Notional"] == pytest.approx(300.0)  # 4 × $75
    # ($80 - $75) × 4 = $20 (not $40 against the agent's $70 intent)
    assert r["Gross P&L"] == pytest.approx(20.0)


def test_load_decisions_empty_log(tmp_state):
    assert dd.load_decisions() == []


def test_cost_today_zero_no_log(tmp_state):
    assert dd.cost_today_usd() == 0.0


def test_load_run_summaries_returns_empty_when_no_runs(tmp_state):
    assert dd.load_run_summaries() == []


def test_load_run_summaries_pulls_rationales_and_funnel(tmp_state):
    """Each summary aggregates portfolio.json (rationales + position count),
    screen/research/scenarios candidate counts, next_run.json, and the run's
    total cost from the cost log. Powers the Cycles tab."""
    # Build a fake run dir with all five artifacts
    rid = "20260512T123456Z-deadbeef"
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True)
    state.write_json(run_dir / "screen.json", {
        "passed": [{"symbol": "TQQQ"}, {"symbol": "UPRO"}, {"symbol": "SOXL"}],
    })
    state.write_json(run_dir / "research.json", {
        "candidates": [{"symbol": "TQQQ"}, {"symbol": "UPRO"}],
    })
    state.write_json(run_dir / "scenarios.json", {
        "candidates": [{"symbol": "TQQQ"}],
    })
    state.write_json(run_dir / "portfolio.json", {
        "run_id": rid, "generated_at": "2026-05-12T12:34:56Z",
        "all_cash": True, "positions": [],
        "all_cash_rationale": "Single positive-EV candidate below threshold.",
        "construction_rationale": "Zero positions taken.",
    })
    state.write_json(run_dir / "next_run.json", {
        "next_run_at": "2026-05-12T14:00:00Z",
        "rationale": "Wait for market open.",
    })
    state.append_cost({
        "run_id": rid, "stage": "screen", "model": "haiku",
        "cost_usd": 0.05, "at": "2026-05-12T12:30:00Z",
    })
    state.append_cost({
        "run_id": rid, "stage": "construct", "model": "opus",
        "cost_usd": 0.20, "at": "2026-05-12T12:34:00Z",
    })

    out = dd.load_run_summaries(limit=10)
    assert len(out) == 1
    s = out[0]
    assert s["run_id"] == rid
    assert s["all_cash"] is True
    assert s["positions_count"] == 0
    assert s["screened_count"] == 3
    assert s["researched_count"] == 2
    assert s["scenarios_count"] == 1
    assert s["all_cash_rationale"].startswith("Single positive-EV candidate")
    assert s["next_run_rationale"] == "Wait for market open."
    assert s["cost_usd"] == pytest.approx(0.25)


def test_load_run_summaries_newest_first(tmp_state):
    """Run dirs are timestamp-prefixed; summary order must be newest first."""
    for rid in ("20260510T100000Z-old", "20260512T100000Z-new", "20260511T100000Z-mid"):
        (state.RUNS_DIR / rid).mkdir(parents=True)
    out = dd.load_run_summaries()
    assert [s["run_id"] for s in out] == [
        "20260512T100000Z-new",
        "20260511T100000Z-mid",
        "20260510T100000Z-old",
    ]


def test_load_run_summaries_tolerates_null_list_fields(tmp_state):
    """Regression for Codex P1 on PR #35: a malformed artifact like
    {"passed": null} previously raised TypeError on len(None), killing
    the entire Cycles tab render. One bad run must not take down
    visibility for all others."""
    bad_rid = "20260512T120000Z-corrupt"
    good_rid = "20260512T130000Z-clean"

    bad_dir = state.RUNS_DIR / bad_rid
    bad_dir.mkdir(parents=True)
    state.write_json(bad_dir / "screen.json", {"passed": None, "rejected": None})
    state.write_json(bad_dir / "research.json", {"candidates": None})
    state.write_json(bad_dir / "portfolio.json", {"positions": None, "all_cash": None})
    state.write_json(bad_dir / "next_run.json", {"next_run_at": None, "rationale": None})

    good_dir = state.RUNS_DIR / good_rid
    good_dir.mkdir(parents=True)
    state.write_json(good_dir / "screen.json", {"passed": [{"symbol": "TQQQ"}]})

    # Must not raise. Both summaries should come back.
    out = dd.load_run_summaries()
    assert len(out) == 2

    # Corrupt run shows zeros for the null fields, not garbage / crash
    corrupt = next(s for s in out if s["run_id"] == bad_rid)
    assert corrupt["screened_count"] == 0
    assert corrupt["researched_count"] == 0
    assert corrupt["scenarios_count"] == 0
    assert corrupt["positions_count"] == 0
    assert corrupt["next_run_rationale"] == ""

    # Clean run still parsed correctly alongside the corrupt one
    clean = next(s for s in out if s["run_id"] == good_rid)
    assert clean["screened_count"] == 1


def test_load_run_summaries_tolerates_top_level_non_dict(tmp_state):
    """Even more degenerate: the artifact is a JSON list/string at the
    top level. Still mustn't crash."""
    rid = "20260512T140000Z-weird"
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True)
    state.write_json(run_dir / "portfolio.json", ["not", "a", "dict"])
    state.write_json(run_dir / "next_run.json", "just a string")
    state.write_json(run_dir / "screen.json", 42)

    out = dd.load_run_summaries()
    assert len(out) == 1
    assert out[0]["run_id"] == rid
    assert out[0]["positions_count"] == 0
    assert out[0]["screened_count"] == 0


def test_load_run_summaries_tolerates_missing_artifacts(tmp_state):
    """A run dir that crashed mid-stage may be missing some artifacts. The
    summary should still come back populated for whatever was written."""
    rid = "20260512T120000Z-partial"
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True)
    # Only screen.json — no portfolio.json or next_run.json
    state.write_json(run_dir / "screen.json", {"passed": [{"symbol": "TQQQ"}]})

    out = dd.load_run_summaries()
    assert len(out) == 1
    s = out[0]
    assert s["run_id"] == rid
    assert s["screened_count"] == 1
    assert s["all_cash"] is None  # never written
    assert s["construction_rationale"] == ""
    assert s["next_run_rationale"] == ""


def test_runs_count_distinct_run_ids(tmp_state):
    """`runs_count()` must count distinct run_ids, not cost-log rows. Codex
    P2 on PR #33: total_token_cost()['calls'] was being mislabelled as
    'Runs all time' on the dashboard. One orchestrator run produces ~6-8
    cost-log rows (one per LLM stage + retries) so 'calls' over-counts
    runs by that factor."""
    # Three runs, 7 stages each → calls=21 but runs=3.
    for run_id in ("r-A", "r-B", "r-C"):
        for stage in ("screen", "research_bull", "research_bear",
                      "scenarios", "construct", "meta", "execute"):
            state.append_cost({
                "run_id": run_id, "stage": stage, "model": "claude-x",
                "cost_usd": 0.01, "at": state.utcnow_iso(),
            })
    assert dd.runs_count() == 3
    assert dd.total_token_cost()["calls"] == 21


def test_runs_count_empty(tmp_state):
    assert dd.runs_count() == 0


def test_cost_today_aggregates(tmp_state):
    state.append_cost({"run_id": "r1", "stage": "screen", "model": "m", "cost_usd": 0.10, "at": state.utcnow_iso()})
    state.append_cost({"run_id": "r2", "stage": "screen", "model": "m", "cost_usd": 0.20, "at": state.utcnow_iso()})
    assert dd.cost_today_usd() == pytest.approx(0.30)


def test_empty_portfolio_when_nothing_anywhere(tmp_state, monkeypatch):
    """If the fixture file is also missing, helper returns a coherent all-cash stub."""
    monkeypatch.setattr(dd, "SEED_PORTFOLIO_FALLBACK", tmp_state / "does-not-exist.json")
    p, src = dd.load_portfolio()
    assert src == "empty"
    assert p["all_cash"] is True
    assert p["positions"] == []


# ---------- token + cost aggregation ----------


def test_total_token_cost_empty(tmp_state):
    t = dd.total_token_cost()
    assert t["calls"] == 0
    assert t["total_tokens"] == 0
    assert t["cost_usd"] == 0.0


def test_total_token_cost_aggregates(tmp_state):
    state.append_cost({
        "run_id": "r1", "stage": "screen", "model": "claude-haiku-4-5",
        "cost_usd": 0.04, "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 800,
        "at": "2026-05-10T12:00:00Z",
    })
    state.append_cost({
        "run_id": "r2", "stage": "scenarios", "model": "claude-sonnet-4-6",
        "cost_usd": 0.18, "input_tokens": 500, "output_tokens": 1500,
        "cache_creation_input_tokens": 200, "cache_read_input_tokens": 0,
        "at": "2026-04-15T09:30:00Z",
    })
    t = dd.total_token_cost()
    assert t["calls"] == 2
    assert t["input_tokens"] == 1500
    assert t["output_tokens"] == 1700
    assert t["cache_creation_input_tokens"] == 200
    assert t["cache_read_input_tokens"] == 800
    assert t["total_tokens"] == 1500 + 1700 + 200 + 800
    assert t["cost_usd"] == pytest.approx(0.22)


def test_cost_by_month_buckets_correctly(tmp_state):
    rows = [
        ("2026-05-10T12:00:00Z", 0.10, 1000),
        ("2026-05-29T23:00:00Z", 0.20, 2000),
        ("2026-04-15T09:30:00Z", 0.30, 3000),
        ("2026-03-01T00:00:01Z", 0.05, 500),
    ]
    for at, cost, tokens in rows:
        state.append_cost({
            "run_id": "x", "stage": "s", "model": "m",
            "cost_usd": cost, "input_tokens": tokens,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "at": at,
        })
    by_month = dd.cost_by_month()
    assert [b["month"] for b in by_month] == ["2026-03", "2026-04", "2026-05"]
    may = next(b for b in by_month if b["month"] == "2026-05")
    assert may["calls"] == 2
    assert may["cost_usd"] == pytest.approx(0.30)
    assert may["total_tokens"] == 3000


def test_cost_by_month_empty(tmp_state):
    assert dd.cost_by_month() == []


# ---------- all-time cost reset display filter ----------


def test_total_token_cost_filters_post_all_time_reset(tmp_state):
    """After state.set_all_time_cost_reset, total_token_cost only counts rows
    AFTER the reset timestamp. Underlying costs.jsonl is unchanged."""
    state.append_cost({
        "run_id": "old", "stage": "x", "model": "m",
        "cost_usd": 0.50, "input_tokens": 100, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "at": "2026-05-10T12:00:00Z",
    })
    # Plant a reset marker AFTER the old row
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    state.append_cost({
        "run_id": "new", "stage": "y", "model": "m",
        "cost_usd": 0.10, "input_tokens": 50, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "at": "2026-05-12T08:00:00Z",
    })
    t = dd.total_token_cost()
    assert t["calls"] == 1, "should only count post-reset call"
    assert t["cost_usd"] == pytest.approx(0.10)
    assert t["input_tokens"] == 50
    # Audit log preserved
    assert state.COSTS_LOG.exists()
    raw_lines = state.COSTS_LOG.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 2


def test_runs_count_filters_post_all_time_reset(tmp_state):
    """A reset wipes pre-reset run_ids from the displayed cycle count."""
    for at, rid in [
        ("2026-05-10T12:00:00Z", "old-1"),
        ("2026-05-10T14:00:00Z", "old-2"),
        ("2026-05-12T08:00:00Z", "new-1"),
    ]:
        state.append_cost({
            "run_id": rid, "stage": "s", "model": "m",
            "cost_usd": 0.10, "at": at,
        })
    assert dd.runs_count() == 3  # before reset
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    assert dd.runs_count() == 1  # only new-1 survives


def test_cost_by_month_filters_post_all_time_reset(tmp_state):
    """Monthly breakdown also honours the all-time reset."""
    state.append_cost({
        "run_id": "x", "stage": "s", "model": "m",
        "cost_usd": 0.30, "at": "2026-04-15T09:30:00Z",
    })
    state.append_cost({
        "run_id": "y", "stage": "s", "model": "m",
        "cost_usd": 0.10, "at": "2026-05-12T08:00:00Z",
    })
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    by_month = dd.cost_by_month()
    assert [b["month"] for b in by_month] == ["2026-05"]
    assert by_month[0]["cost_usd"] == pytest.approx(0.10)


def test_cost_for_run_usd_filters_post_all_time_reset(tmp_state):
    """A reset zeros an in-flight run's displayed cost so the "this run"
    meter on the dashboard matches user expectation, but the raw log
    used by cap enforcement (state.read_costs_for_run) is preserved."""
    state.append_cost({
        "run_id": "in-flight", "stage": "screen", "model": "m",
        "cost_usd": 0.40, "at": "2026-05-12T08:00:00Z",
    })
    assert dd.cost_for_run_usd("in-flight") == pytest.approx(0.40)
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-12T09:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    # Display: zero
    assert dd.cost_for_run_usd("in-flight") == pytest.approx(0.0)
    # Raw log (cap enforcement path): preserved
    rows = state.read_costs_for_run("in-flight")
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(0.40)


def test_try_load_broker_marks_no_keys_returns_empty(tmp_state, monkeypatch):
    """Without ALPACA_API_KEY / SECRET in env, AlpacaBroker init raises and
    the dashboard helper must absorb the failure so the page still renders."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert dd.try_load_broker_marks() == {}


def test_load_nav_history_round_trip(tmp_state):
    assert dd.load_nav_history() == []
    state.append_nav({"run_id": "r1", "at": state.utcnow_iso(), "nav_usd": 2500.0})
    state.append_nav({"run_id": "r2", "at": state.utcnow_iso(), "nav_usd": 2520.0})
    hist = dd.load_nav_history()
    assert len(hist) == 2
    assert hist[1]["nav_usd"] == 2520.0
