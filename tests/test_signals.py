"""Tests for lib/signals — v2 pipeline stage 1.

Deterministic feature generator. The functions under test don't hit
the network: market_data.history is monkey-patched per test.
"""
from __future__ import annotations

import math

import pandas as pd

from lib import signals, universe


def _flat_history(symbol: str, *, last_close: float = 100.0, n: int = 252) -> pd.DataFrame:
    """Pure flat history — closes constant at last_close, volume 1M.
    Used to assert null returns and zero-vol shapes."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Close": [last_close] * n, "Volume": [1_000_000] * n},
        index=idx,
    )


def _trending_history(symbol: str, *, start: float = 50.0, end: float = 60.0, n: int = 252) -> pd.DataFrame:
    """Linearly trending history — close goes start → end over n days.
    Momentum should be positive (end > start)."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    closes = [start + (end - start) * i / (n - 1) for i in range(n)]
    return pd.DataFrame({"Close": closes, "Volume": [1_000_000] * n}, index=idx)


def test_row_for_symbol_unknown_symbol_returns_error_row():
    row = signals._row_for_symbol("NOT_A_REAL_TICKER", run_id=None)
    assert row.error == "symbol not in universe"
    assert row.last_close is None


def test_row_for_symbol_history_failure_returns_error_row(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(signals.market_data, "history", _boom)
    row = signals._row_for_symbol("TQQQ", run_id=None)
    assert row.error is not None
    assert "yfinance down" in row.error
    assert row.last_close is None
    assert row.kind == "etf"  # universe metadata still populated


def test_row_for_symbol_empty_history_returns_error_row(monkeypatch):
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: pd.DataFrame(),
    )
    row = signals._row_for_symbol("TQQQ", run_id=None)
    assert row.error == "no history available"


def test_row_for_symbol_flat_history_produces_zero_momentum(monkeypatch):
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _flat_history(req.symbol),
    )
    row = signals._row_for_symbol("TQQQ", run_id=None)
    assert row.error is None
    assert row.last_close == 100.0
    assert row.momentum_30d_pct == 0.0
    assert row.momentum_60d_pct == 0.0
    # Flat closes → returns std() = 0 → annualised vol = 0.
    assert row.hv_30d_annualised == 0.0
    assert row.dist_from_50d_ma_pct == 0.0


def test_row_for_symbol_trending_history_produces_positive_momentum(monkeypatch):
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _trending_history(req.symbol, start=50.0, end=60.0),
    )
    row = signals._row_for_symbol("TQQQ", run_id=None)
    assert row.momentum_30d_pct is not None and row.momentum_30d_pct > 0
    assert row.momentum_60d_pct is not None and row.momentum_60d_pct > 0
    # Above the 200d MA on a 252-day uptrend.
    assert row.dist_from_50d_ma_pct is not None
    assert row.dist_from_200d_ma_pct is not None and row.dist_from_200d_ma_pct > 0


def test_rows_are_etf_only(monkeypatch):
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _flat_history(req.symbol),
    )
    tqqq = signals._row_for_symbol("TQQQ", run_id=None)
    assert tqqq.kind == "etf"
    # No option metadata is carried on signal rows anymore.
    assert not hasattr(tqqq, "is_optionable")
    assert "is_optionable" not in tqqq.to_dict()


def test_compute_signals_iterates_full_universe_by_default(monkeypatch):
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _flat_history(req.symbol),
    )
    out = signals.compute_signals(run_id="rid")
    assert out["run_id"] == "rid"
    assert "generated_at" in out
    assert len(out["tickers"]) == len(universe.UNIVERSE)
    syms = {t["symbol"] for t in out["tickers"]}
    assert syms == set(universe.all_symbols())


def test_compute_signals_respects_symbols_argument(monkeypatch):
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _flat_history(req.symbol),
    )
    out = signals.compute_signals(run_id="rid", symbols=["TQQQ", "SQQQ"])
    assert len(out["tickers"]) == 2
    assert {t["symbol"] for t in out["tickers"]} == {"TQQQ", "SQQQ"}


def test_compute_signals_output_validates_against_schema(monkeypatch, tmp_state):
    """Sanity check the output shape against schemas/signals.schema.json."""
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _flat_history(req.symbol),
    )
    out = signals.compute_signals(run_id="rid")
    from lib import state
    state.validate(out, "signals.schema.json")


def test_no_optionable_metadata_emitted(monkeypatch):
    """ETF-only: signal rows carry no `is_optionable` field and the module
    exposes no OPTIONABLE_SYMBOLS constant."""
    monkeypatch.setattr(
        signals.market_data, "history",
        lambda req, **kw: _flat_history(req.symbol),
    )
    assert not hasattr(signals, "OPTIONABLE_SYMBOLS")
    out = signals.compute_signals(run_id="rid", symbols=["TQQQ"])
    assert "is_optionable" not in out["tickers"][0]
