"""yfinance wrappers — daily/intraday history, ADV, basic fundamentals.

Lazy-imports yfinance so unit tests can run without it. Caches per-run under
state/runs/{run_id}/cache/ to avoid re-fetching the same series within a single
pipeline run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .state import RUNS_DIR

if TYPE_CHECKING:
    import pandas as pd  # noqa: F401


@dataclass(frozen=True)
class HistoryRequest:
    symbol: str
    period: str = "6mo"   # yfinance period string
    interval: str = "1d"  # yfinance interval string


def _cache_path(run_id: str | None, key: str) -> Path | None:
    if run_id is None:
        return None
    d = RUNS_DIR / run_id / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.parquet"


def _yf():
    import yfinance as yf  # noqa: WPS433
    return yf


def history(req: HistoryRequest, *, run_id: str | None = None) -> "pd.DataFrame":
    import pandas as pd  # noqa: WPS433
    cp = _cache_path(run_id, f"hist_{req.symbol}_{req.period}_{req.interval}")
    if cp is not None and cp.exists():
        return pd.read_parquet(cp)
    yf = _yf()
    df = yf.Ticker(req.symbol).history(period=req.period, interval=req.interval, auto_adjust=False)
    if cp is not None and not df.empty:
        try:
            df.to_parquet(cp)
        except Exception:
            cp.with_suffix(".json").write_text(df.reset_index().to_json(orient="records"))
    return df


def average_daily_volume(symbol: str, *, lookback_days: int = 30, run_id: str | None = None) -> float:
    df = history(HistoryRequest(symbol=symbol, period=f"{max(lookback_days, 30)}d"), run_id=run_id)
    if df.empty or "Volume" not in df.columns:
        return 0.0
    return float(df["Volume"].tail(lookback_days).mean())


def historical_volatility(symbol: str, *, lookback_days: int = 30, run_id: str | None = None) -> float:
    """Annualised close-to-close HV (252 trading days)."""
    df = history(HistoryRequest(symbol=symbol, period=f"{max(lookback_days*2, 90)}d"), run_id=run_id)
    if df.empty:
        return 0.0
    returns = df["Close"].pct_change().tail(lookback_days).dropna()
    if returns.empty:
        return 0.0
    return float(returns.std() * (252 ** 0.5))


def passes_liquidity_filter(
    symbol: str,
    *,
    min_adv: float = 1_000_000,
    run_id: str | None = None,
) -> bool:
    return average_daily_volume(symbol, run_id=run_id) >= min_adv


def universe_snapshot(symbols: Iterable[str], *, run_id: str | None = None) -> list[dict]:
    """Compact per-symbol summary for prompt-cached universe block."""
    out = []
    for s in symbols:
        try:
            adv = average_daily_volume(s, run_id=run_id)
            hv = historical_volatility(s, run_id=run_id)
        except Exception as e:
            out.append({"symbol": s, "error": str(e)})
            continue
        out.append({"symbol": s, "adv": adv, "hv_annualised": hv})
    return out
