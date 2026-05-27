"""yfinance wrappers — daily/intraday history, ADV, HV, full universe snapshot.

Lazy-imports yfinance so unit tests can run without it. Caches per-run under
state/runs/{run_id}/cache/ to avoid re-fetching the same series within a single
pipeline run. Universe snapshot fans out per-symbol fetches across threads
(yfinance releases the GIL during network IO) so a 16-symbol universe finishes
in ~5 seconds instead of ~30 sequentially.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from . import state as _state
from .universe import by_symbol

if TYPE_CHECKING:
    import pandas as pd  # noqa: F401


@dataclass(frozen=True)
class HistoryRequest:
    symbol: str
    period: str = "6mo"   # yfinance period string
    interval: str = "1d"  # yfinance interval string


def _cache_path(run_id: str | None, key: str) -> Path | None:
    """Look up RUNS_DIR dynamically through the state module so tmp_state's
    monkeypatch is honoured in tests (a direct `from .state import RUNS_DIR`
    captures the path at import-time, before the fixture runs)."""
    if run_id is None:
        return None
    d = _state.RUNS_DIR / run_id / "cache"
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


def total_return_history(
    symbol: str,
    *,
    start,
    end,
    run_id: str | None = None,
) -> "pd.DataFrame":
    """Daily total-return history (dividends reinvested) between two dates.

    Uses ``auto_adjust=True`` so the ``Close`` column is split- and
    dividend-adjusted — i.e. equivalent to reinvesting dividends. Used
    by ``lib.benchmark`` for the SPY benchmark comparison.

    Both ``start`` and ``end`` are treated as INCLUSIVE — internally we
    pass ``end + 1 day`` to yfinance because its ``history(end=...)``
    parameter is exclusive (verified in PriceHistory.history docstring
    of the installed yfinance). Without this bump the most recent
    trading day is silently dropped, which on a brand-new install with
    only 2 days of NAV history collapses the inner-join to length 1
    and keeps the dashboard stuck in the empty state.

    Caches per ``run_id`` identically to ``history()``; in the dashboard
    context ``run_id`` is None and Streamlit's ``@st.cache_data`` handles
    caching at the call site.
    """
    import datetime as _dt  # noqa: WPS433
    import pandas as pd  # noqa: WPS433

    end_obj = end if isinstance(end, _dt.date) else _dt.date.fromisoformat(str(end))
    start_s = str(start)
    end_inclusive_s = str(end)
    end_for_yf_s = (end_obj + _dt.timedelta(days=1)).isoformat()
    # Cache key uses the inclusive end so two callers asking for the same
    # logical date range share an entry, regardless of internal bumping.
    cp = _cache_path(run_id, f"tr_{symbol}_{start_s}_{end_inclusive_s}")
    if cp is not None and cp.exists():
        return pd.read_parquet(cp)
    yf = _yf()
    df = yf.Ticker(symbol).history(start=start_s, end=end_for_yf_s, auto_adjust=True)
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


def _snapshot_one(symbol: str, *, run_id: str | None) -> dict:
    """Per-symbol live data block. Tolerant — partial failures leave the
    affected field as None rather than crashing the whole universe fetch.

    The static metadata (kind/leverage_factor/family/factor) comes from
    lib.universe; live fields (closes, ADV, HV) come from yfinance below.
    Codex P1 on PR #31: `factor` must be included here, not just in
    universe.metadata_block(), because stage_screen serializes THIS output
    (not metadata_block) when building the screener payload.
    """
    meta = by_symbol(symbol)
    entry: dict = {
        "symbol": symbol,
        "kind": meta.kind if meta else "unknown",
        "leverage_factor": meta.leverage_factor if meta else 1.0,
        "family": meta.family if meta else "",
        "factor": meta.factor if meta else "unknown",
    }
    try:
        df = history(
            HistoryRequest(symbol=symbol, period="6mo"), run_id=run_id
        )
        if df.empty:
            entry.update({
                "last_close": None, "adv_30d": None, "hv_30d_annualised": None,
                "high_52w": None, "low_52w": None, "error": "no history",
            })
            return entry
        closes = df["Close"]
        vols = df["Volume"]
        last = float(closes.iloc[-1])
        adv = float(vols.tail(30).mean()) if "Volume" in df.columns else None
        rets = closes.pct_change().tail(30).dropna()
        hv = float(rets.std() * math.sqrt(252)) if not rets.empty else None
        hi = float(closes.tail(252).max())
        lo = float(closes.tail(252).min())
        entry.update({
            "last_close": round(last, 2),
            "adv_30d": int(adv) if adv is not None else None,
            "hv_30d_annualised": round(hv, 4) if hv is not None else None,
            "high_52w": round(hi, 2),
            "low_52w": round(lo, 2),
            "pct_off_52w_high": round((last / hi - 1) * 100, 2),
        })
    except Exception as e:
        entry["error"] = f"{type(e).__name__}: {e}"
    return entry


def universe_snapshot(
    symbols: Iterable[str],
    *,
    run_id: str | None = None,
    max_workers: int = 8,
) -> list[dict]:
    """Compact per-symbol summary for the screener's universe block.

    Fans out yfinance calls across `max_workers` threads. yfinance releases
    the GIL during network IO, so threading is genuinely parallel. Results
    are returned in the SAME order as the input `symbols` so the screener
    sees a stable list across runs (helps with prompt caching downstream).

    Per-symbol failures are surfaced as {symbol, kind, error: "..."} rather
    than raising — one flaky ticker shouldn't kill the whole screen.
    """
    syms = list(symbols)
    if not syms:
        return []

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_snapshot_one, s, run_id=run_id): s for s in syms}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results[s] = fut.result()
            except Exception as e:
                results[s] = {"symbol": s, "error": f"{type(e).__name__}: {e}"}

    return [results[s] for s in syms]
