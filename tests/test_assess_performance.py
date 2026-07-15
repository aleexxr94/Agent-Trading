"""Tests for the read-only performance-assessment report generator.

Exercises the empty-state degrade path, a populated end-to-end gather,
the low-sample banner, `--json` shape stability, and the read-only
invariant (the run must not write into state/). Network-dependent SPY
metrics are stubbed so the tests are deterministic and offline-safe.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bin import assess_performance
from lib import dashboard_data, state


def _snapshot(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """benchmark_view() hits yfinance — stub it so tests are offline-safe.
    The script already degrades on failure; stubbing just makes the
    returns/risk section deterministic rather than network-dependent."""
    monkeypatch.setattr(dashboard_data, "benchmark_view", lambda *a, **k: None)


def _seed_populated_state() -> str:
    """Write a small but realistic slice of state: one closed TQQQ trade,
    a couple of nav rows, a cost row, an ok decision, and one run dir."""
    run_id = "20260701T140000Z-aaa111"
    # A closed round-trip: buy 2 @ 50, sell 2 @ 55 => +$10 gross.
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 2, "fill_price": 50.0,
        "fees_usd": 0.0, "filled_at": "2026-07-01T14:05:00Z", "run_id": run_id,
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 2, "fill_price": 55.0,
        "fees_usd": 0.0, "filled_at": "2026-07-02T14:05:00Z", "run_id": run_id,
    })
    state.append_nav({
        "run_id": run_id, "at": "2026-07-01T14:00:00Z", "nav_usd": 2500.0,
        "cash_usd": 2400.0, "positions_count": 1, "all_cash": False,
    })
    state.append_nav({
        "run_id": "20260702T140000Z-bbb222", "at": "2026-07-02T14:00:00Z",
        "nav_usd": 2510.0, "cash_usd": 2510.0, "positions_count": 0,
        "all_cash": True,
    })
    state.append_cost({
        "run_id": run_id, "stage": "construct", "model": "opus",
        "cost_usd": 0.20, "at": "2026-07-01T14:00:05Z",
        "input_tokens": 1000, "output_tokens": 200,
        "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0,
    })
    state.append_decision({
        "run_id": run_id, "stage": "construct", "model": "opus",
        "inputs_hash": "abcdef0123456789", "output_ref": "portfolio.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.20,
        "started_at": state.utcnow_iso(), "ended_at": state.utcnow_iso(),
        "status": "ok", "risk_warning": "leveraged ETF risk",
    })
    run_dir = state.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "view.json").write_text(json.dumps({
        "regime": "risk_on", "regime_rationale": "x",
        "candidates": [{"symbol": "TQQQ", "instrument_kind": "etf",
                        "thesis": "t", "confidence": 0.8}],
    }))
    (run_dir / "portfolio.json").write_text(json.dumps({
        "all_cash": False, "positions": [{"symbol": "TQQQ"}],
        "construction_rationale": "r",
    }))
    (run_dir / "sanity.json").write_text(json.dumps({
        "status": "pass", "summary": {"pass": 5, "warn": 0, "fail": 0, "skip": 0},
    }))
    (run_dir / "next_run.json").write_text(json.dumps({
        "run_id": run_id, "next_run_at": "2026-07-01T18:00:00Z",
    }))
    return run_id


def test_empty_state_degrades_without_raising(tmp_state):
    report = assess_performance.gather()
    assert report["meta"]["closed_trades"] == 0
    assert report["meta"]["low_sample"] is True
    # Every section present; none is an unhandled error string.
    for key in ("promotion_scorecard", "returns_risk", "trade_record",
                "calibration", "cost_health", "operational", "cycles"):
        assert key in report
    # Renders without raising.
    md = assess_performance._render(report)
    assert "Performance Assessment" in md
    assert "LOW SAMPLE" in md


def test_empty_state_writes_nothing(tmp_state):
    before = _snapshot(tmp_state)
    assess_performance.gather()
    assert _snapshot(tmp_state) == before


def test_populated_gather_and_render(tmp_state):
    _seed_populated_state()
    report = assess_performance.gather()

    assert report["meta"]["closed_trades"] == 1
    assert report["meta"]["era"]["mode"] == "paper"

    # Scorecard rows come straight from readiness_scorecard.
    crits = {r["criterion"] for r in report["promotion_scorecard"]}
    assert any("Sharpe" in c for c in crits)

    # The closed TQQQ round-trip shows up net-positive under the nasdaq factor.
    stats = report["trade_record"]["stats"]
    assert stats is not None
    assert stats["wins"] == 1
    factors = {r["factor"] for r in report["calibration"]["by_factor"]}
    assert "nasdaq" in factors

    # Cost + cycle sections populated.
    assert report["cost_health"]["token_totals"]["cost_usd"] == pytest.approx(0.20)
    assert len(report["cycles"]) == 1

    md = assess_performance._render(report)
    assert "TQQQ" in md
    assert "§11 Promotion scorecard" in md


def test_populated_gather_writes_nothing(tmp_state):
    _seed_populated_state()
    before = _snapshot(tmp_state)
    assess_performance.gather()
    assert _snapshot(tmp_state) == before


def test_json_output_is_serialisable_and_stable(tmp_state, tmp_path):
    _seed_populated_state()
    out = tmp_path / "assessment.json"
    rc = assess_performance.main(["--json", str(out)])
    assert rc == 0
    loaded = json.loads(out.read_text())
    # Stable top-level shape the Claude layer / a timer can rely on.
    assert set(loaded.keys()) == {
        "meta", "promotion_scorecard", "returns_risk", "trade_record",
        "calibration", "cost_health", "operational", "cycles",
    }


def test_low_sample_clears_with_enough_history(tmp_state, monkeypatch):
    # 30 closed trades over a >28-day span should clear the low-sample gate.
    base_day = 1
    for i in range(30):
        day = base_day + i
        rid = f"202606{day:02d}T140000Z-r{i:04d}"
        state.append_trade({
            "activity_id": f"b{i}", "alpaca_order_id": f"ob{i}", "symbol": "TQQQ",
            "kind": "etf", "side": "buy", "qty": 1, "fill_price": 50.0,
            "fees_usd": 0.0, "filled_at": f"2026-06-{day:02d}T14:05:00Z",
            "run_id": rid,
        })
        state.append_trade({
            "activity_id": f"s{i}", "alpaca_order_id": f"os{i}", "symbol": "TQQQ",
            "kind": "etf", "side": "sell", "qty": 1, "fill_price": 51.0,
            "fees_usd": 0.0, "filled_at": f"2026-06-{day:02d}T15:05:00Z",
            "run_id": rid,
        })
    state.append_nav({"run_id": "r0", "at": "2026-06-01T14:00:00Z", "nav_usd": 2500.0})
    state.append_nav({"run_id": "r1", "at": "2026-06-30T14:00:00Z", "nav_usd": 2600.0})
    report = assess_performance.gather()
    assert report["meta"]["closed_trades"] == 30
    assert report["meta"]["days_running"] >= 28
    assert report["meta"]["low_sample"] is False
