"""Deterministic per-ticker feature generator — v2 pipeline stage 1.

Replaces the v1 LLM screener + bull/bear research + scenarios stages
(which produced text that ultimately compressed to a handful of numbers
anyway) with a pure Python feature table. The strategist + constructor
LLM calls downstream read THIS as their primary input.

For each ticker in lib.universe.UNIVERSE we compute:
  - last_close, adv_30d, kind, factor, leverage_factor — straight from
    market_data.universe_snapshot
  - momentum_30d_pct, momentum_60d_pct — trailing total return
  - hv_30d_annualised, hv_90d_annualised — close-to-close vol
  - dist_from_50d_ma_pct, dist_from_200d_ma_pct — distance from moving
    averages (negative = below MA = downtrend)
  - is_optionable — true for SPY/QQQ/TLT; false otherwise

Per-ticker errors don't kill the run — the row is included with `error`
set and downstream stages skip it.

Per-cycle cost: $0 — yfinance is free, no LLM calls.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from . import market_data, state, universe


# Tickers in the v2 universe that have option chains the constructor can
# trade. Mirrors UniverseEntry.kind == "option_underlying", just hoisted
# to a constant for easy reference.
OPTIONABLE_SYMBOLS: frozenset[str] = frozenset({
    e.symbol for e in universe.UNIVERSE if e.kind == "option_underlying"
})


@dataclass(frozen=True)
class FeatureRow:
    symbol: str
    kind: str
    factor: str
    leverage_factor: float
    family: str
    last_close: float | None
    adv_30d: int | None
    momentum_30d_pct: float | None
    momentum_60d_pct: float | None
    hv_30d_annualised: float | None
    hv_90d_annualised: float | None
    dist_from_50d_ma_pct: float | None
    dist_from_200d_ma_pct: float | None
    is_optionable: bool
    error: str | None = None

    def to_dict(self) -> dict:
        d = {
            "symbol": self.symbol,
            "kind": self.kind,
            "factor": self.factor,
            "leverage_factor": self.leverage_factor,
            "family": self.family,
            "last_close": self.last_close,
            "adv_30d": self.adv_30d,
            "momentum_30d_pct": self.momentum_30d_pct,
            "momentum_60d_pct": self.momentum_60d_pct,
            "hv_30d_annualised": self.hv_30d_annualised,
            "hv_90d_annualised": self.hv_90d_annualised,
            "dist_from_50d_ma_pct": self.dist_from_50d_ma_pct,
            "dist_from_200d_ma_pct": self.dist_from_200d_ma_pct,
            "is_optionable": self.is_optionable,
        }
        if self.error is not None:
            d["error"] = self.error
        return d


def _trailing_return_pct(closes: pd.Series, lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    start = closes.iloc[-(lookback + 1)]
    end = closes.iloc[-1]
    if start is None or start == 0 or pd.isna(start):
        return None
    return float((end / start - 1.0) * 100.0)


def _annualised_vol(returns: pd.Series, lookback: int) -> float | None:
    tail = returns.tail(lookback).dropna()
    if len(tail) < max(5, lookback // 4):
        return None
    return float(tail.std() * math.sqrt(252))


def _dist_from_ma_pct(closes: pd.Series, window: int) -> float | None:
    if len(closes) < window:
        return None
    ma = closes.tail(window).mean()
    last = closes.iloc[-1]
    if ma is None or ma == 0 or pd.isna(ma):
        return None
    return float((last / ma - 1.0) * 100.0)


def _row_for_symbol(symbol: str, *, run_id: str | None) -> FeatureRow:
    """Compute the full feature row for one ticker. Returns a row with
    ``error`` set if the data fetch fails — never raises."""
    entry = universe.by_symbol(symbol)
    if entry is None:
        return FeatureRow(
            symbol=symbol, kind="unknown", factor="unknown",
            leverage_factor=1.0, family="",
            last_close=None, adv_30d=None,
            momentum_30d_pct=None, momentum_60d_pct=None,
            hv_30d_annualised=None, hv_90d_annualised=None,
            dist_from_50d_ma_pct=None, dist_from_200d_ma_pct=None,
            is_optionable=False, error="symbol not in universe",
        )

    try:
        df = market_data.history(
            market_data.HistoryRequest(symbol=symbol, period="1y"),
            run_id=run_id,
        )
    except Exception as e:
        return FeatureRow(
            symbol=symbol, kind=entry.kind, factor=entry.factor,
            leverage_factor=entry.leverage_factor, family=entry.family,
            last_close=None, adv_30d=None,
            momentum_30d_pct=None, momentum_60d_pct=None,
            hv_30d_annualised=None, hv_90d_annualised=None,
            dist_from_50d_ma_pct=None, dist_from_200d_ma_pct=None,
            is_optionable=symbol in OPTIONABLE_SYMBOLS,
            error=f"{type(e).__name__}: {e}",
        )

    if df is None or df.empty or "Close" not in df.columns:
        return FeatureRow(
            symbol=symbol, kind=entry.kind, factor=entry.factor,
            leverage_factor=entry.leverage_factor, family=entry.family,
            last_close=None, adv_30d=None,
            momentum_30d_pct=None, momentum_60d_pct=None,
            hv_30d_annualised=None, hv_90d_annualised=None,
            dist_from_50d_ma_pct=None, dist_from_200d_ma_pct=None,
            is_optionable=symbol in OPTIONABLE_SYMBOLS,
            error="no history available",
        )

    closes = df["Close"]
    returns = closes.pct_change()
    vols = df["Volume"] if "Volume" in df.columns else pd.Series(dtype=float)
    last_close = float(closes.iloc[-1])
    adv_30d = int(vols.tail(30).mean()) if not vols.empty else None

    return FeatureRow(
        symbol=symbol,
        kind=entry.kind,
        factor=entry.factor,
        leverage_factor=entry.leverage_factor,
        family=entry.family,
        last_close=round(last_close, 4),
        adv_30d=adv_30d,
        momentum_30d_pct=_round(_trailing_return_pct(closes, 30), 2),
        momentum_60d_pct=_round(_trailing_return_pct(closes, 60), 2),
        hv_30d_annualised=_round(_annualised_vol(returns, 30), 4),
        hv_90d_annualised=_round(_annualised_vol(returns, 90), 4),
        dist_from_50d_ma_pct=_round(_dist_from_ma_pct(closes, 50), 2),
        dist_from_200d_ma_pct=_round(_dist_from_ma_pct(closes, 200), 2),
        is_optionable=symbol in OPTIONABLE_SYMBOLS,
    )


def _round(v: float | None, ndigits: int) -> float | None:
    return None if v is None else round(v, ndigits)


def compute_signals(*, run_id: str, symbols: Iterable[str] | None = None) -> dict:
    """Compute the v2 signals table. Schema: signals.schema.json.

    By default iterates the full v2 universe. ``symbols`` lets callers
    constrain to a subset (used by tests).
    """
    syms = list(symbols) if symbols is not None else universe.all_symbols()
    rows = [_row_for_symbol(s, run_id=run_id) for s in syms]
    return {
        "run_id": run_id,
        "generated_at": state.utcnow_iso(),
        "tickers": [r.to_dict() for r in rows],
    }
