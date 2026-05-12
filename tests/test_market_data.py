"""Tests for lib.market_data.universe_snapshot.

We never call real yfinance from the test suite — monkeypatch the lazy
import so the test is fast, deterministic, and offline-safe.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta

import pytest

from lib import market_data


def _fake_yfinance(close_series=None, volume_series=None, empty=False):
    """Build a fake yfinance module whose Ticker(s).history() returns a
    DataFrame-ish object with the Close + Volume columns we care about."""
    import pandas as pd

    if empty:
        df = pd.DataFrame()
    else:
        n = 260  # ~1 trading year
        idx = pd.date_range(end=datetime.utcnow(), periods=n, freq="B")
        closes = close_series if close_series is not None else [100 + (i * 0.1) for i in range(n)]
        volumes = volume_series if volume_series is not None else [5_000_000] * n
        df = pd.DataFrame({"Close": closes, "Volume": volumes}, index=idx)

    class _FakeTicker:
        def __init__(self, sym):
            self.sym = sym
        def history(self, period="6mo", interval="1d", auto_adjust=False):
            return df.copy()

    fake = types.ModuleType("yfinance")
    fake.Ticker = _FakeTicker
    return fake


@pytest.fixture
def fake_yf(monkeypatch):
    fake = _fake_yfinance()
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    return fake


def test_universe_snapshot_returns_one_row_per_symbol(fake_yf, tmp_state):
    snap = market_data.universe_snapshot(["TQQQ", "SOXL", "SPY"], run_id="test-run")
    assert [r["symbol"] for r in snap] == ["TQQQ", "SOXL", "SPY"]


def test_snapshot_includes_metadata_from_universe(fake_yf, tmp_state):
    """Static metadata fields must flow through to the per-symbol snapshot.
    Codex P1 on PR #31: `factor` must be present here (not just in
    universe.metadata_block) because stage_screen serializes the snapshot
    output as the screener LLM's input."""
    snap = market_data.universe_snapshot(["TQQQ"], run_id="test-run")
    row = snap[0]
    assert row["kind"] == "etf"
    assert row["leverage_factor"] == 3.0
    assert "Nasdaq" in row["family"]
    assert row["factor"] == "nasdaq"


def test_snapshot_factor_shared_across_bull_bear_pair(fake_yf, tmp_state):
    """Bull/bear pairs share a factor in the snapshot too — same invariant
    as the static universe, but verified through the live data pipeline."""
    snap = market_data.universe_snapshot(["TQQQ", "SQQQ"], run_id="test-run")
    assert {r["symbol"]: r["factor"] for r in snap} == {
        "TQQQ": "nasdaq", "SQQQ": "nasdaq",
    }


def test_snapshot_computes_price_volume_volatility(fake_yf, tmp_state):
    snap = market_data.universe_snapshot(["TQQQ"], run_id="test-run")
    row = snap[0]
    assert row["last_close"] is not None and row["last_close"] > 0
    assert row["adv_30d"] == 5_000_000  # matches our fixture volume
    assert row["hv_30d_annualised"] is not None
    assert row["hv_30d_annualised"] >= 0
    assert row["high_52w"] >= row["last_close"] >= row["low_52w"]
    assert "pct_off_52w_high" in row


def test_snapshot_handles_empty_history(monkeypatch, tmp_state):
    fake = _fake_yfinance(empty=True)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    snap = market_data.universe_snapshot(["TQQQ"], run_id="test-run")
    row = snap[0]
    assert row["last_close"] is None
    assert row.get("error") == "no history"
    # Metadata still present
    assert row["symbol"] == "TQQQ"


def test_snapshot_preserves_input_order(fake_yf, tmp_state):
    """Order matters for prompt-cache stability — the universe block sent to
    the screener should be deterministic across runs."""
    syms = ["SOXL", "TQQQ", "FAS", "SPY", "QQQ"]
    snap = market_data.universe_snapshot(syms, run_id="test-run")
    assert [r["symbol"] for r in snap] == syms


def test_snapshot_empty_universe_is_empty():
    assert market_data.universe_snapshot([], run_id=None) == []


def test_snapshot_isolated_failure_does_not_break_others(monkeypatch, tmp_state):
    """One symbol's network blowup leaves an error row but everything else
    completes normally."""
    good = _fake_yfinance()

    class _MixedTicker:
        def __init__(self, sym):
            self.sym = sym
        def history(self, **kw):
            if self.sym == "TQQQ":
                raise RuntimeError("simulated yfinance 500")
            return good.Ticker(self.sym).history(**kw)

    mixed = types.ModuleType("yfinance")
    mixed.Ticker = _MixedTicker
    monkeypatch.setitem(sys.modules, "yfinance", mixed)

    snap = market_data.universe_snapshot(["TQQQ", "SOXL"], run_id="test-run")
    failed = next(r for r in snap if r["symbol"] == "TQQQ")
    ok = next(r for r in snap if r["symbol"] == "SOXL")
    assert "error" in failed
    assert "RuntimeError" in failed["error"]
    assert ok["last_close"] is not None
    assert "error" not in ok
