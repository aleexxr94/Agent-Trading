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
  - rsi_14 — 14-day RSI (Cutler's SMA variant; deterministic, no seed)
  - rel_strength_spy_30d — ticker 30d return minus SPY 30d return, so
    the strategist can see "up but lagging the tape" vs true leadership
  - trend_r2 — R² of close vs time over 60 sessions: ~1 = clean trend,
    ~0 = chop. Directly relevant to leveraged-ETF decay, which bleeds
    in chop even when the underlying ends flat.

Plus one universe-level block:
  - factor_correlations — pairwise 30d return correlation between factor
    bull ETFs (|corr| ≥ 0.7 only), so the LLM stages can SEE which
    factors are currently the same bet (e.g. nasdaq/semis) and which are
    genuinely independent diversifiers — instead of relying on a static
    "tech is correlated" prompt rule.

Per-ticker errors don't kill the run — the row is included with `error`
set and downstream stages skip it.

Per-cycle cost: $0 — yfinance is free, no LLM calls.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from . import events, market_data, state, universe

# Correlation block parameters. 30 sessions matches the momentum/HV
# short window; 0.7 keeps the block to the pairs that actually matter
# (and keeps the LLM payload small).
CORR_LOOKBACK_SESSIONS = 30
CORR_MIN_ABS = 0.7
CORR_MAX_ROWS = 12


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
    rsi_14: float | None
    rel_strength_spy_30d: float | None
    trend_r2: float | None
    upcoming_macro_events_7d: list[dict]
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
            "rsi_14": self.rsi_14,
            "rel_strength_spy_30d": self.rel_strength_spy_30d,
            "trend_r2": self.trend_r2,
            "upcoming_macro_events_7d": self.upcoming_macro_events_7d,
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


def _rsi(closes: pd.Series, window: int = 14) -> float | None:
    """Cutler's RSI (simple-average gains/losses). Deterministic and
    path-independent over the window, unlike Wilder's recursive form."""
    if len(closes) <= window:
        return None
    diffs = closes.diff().tail(window).dropna()
    if len(diffs) < window:
        return None
    gains = diffs.clip(lower=0.0).mean()
    losses = (-diffs.clip(upper=0.0)).mean()
    if pd.isna(gains) or pd.isna(losses):
        return None
    if losses == 0:
        # No down days: pegged-strong (100) — unless the tape was dead
        # flat (no up days either), which is neutral, not strong.
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return float(100.0 - 100.0 / (1.0 + rs))


def _trend_r2(closes: pd.Series, window: int = 60) -> float | None:
    """R² of close vs time over the trailing window. ~1 = clean trend,
    ~0 = chop (where daily-rebalanced leveraged ETFs decay)."""
    tail = closes.tail(window).dropna()
    if len(tail) < max(20, window // 2):
        return None
    y = tail.to_numpy(dtype=float)
    x = pd.Series(range(len(y)), dtype=float)
    corr = pd.Series(y).corr(x)
    if corr is None or pd.isna(corr):
        return None
    return float(corr * corr)


def _empty_row(entry, symbol: str, macro_events: list[dict], error: str) -> FeatureRow:
    return FeatureRow(
        symbol=symbol,
        kind=entry.kind if entry else "unknown",
        factor=entry.factor if entry else "unknown",
        leverage_factor=entry.leverage_factor if entry else 1.0,
        family=entry.family if entry else "",
        last_close=None, adv_30d=None,
        momentum_30d_pct=None, momentum_60d_pct=None,
        hv_30d_annualised=None, hv_90d_annualised=None,
        dist_from_50d_ma_pct=None, dist_from_200d_ma_pct=None,
        rsi_14=None, rel_strength_spy_30d=None, trend_r2=None,
        upcoming_macro_events_7d=macro_events,
        error=error,
    )


def _row_for_symbol(
    symbol: str, *, run_id: str | None, spy_return_30d_pct: float | None = None,
) -> tuple[FeatureRow, pd.Series | None]:
    """Compute the full feature row for one ticker. Returns (row, closes);
    the closes series feeds the factor-correlation block. Returns a row
    with ``error`` set (and closes=None) if the data fetch fails — never
    raises."""
    entry = universe.by_symbol(symbol)
    macro_events = events.events_for_symbol(symbol)
    if entry is None:
        return _empty_row(None, symbol, macro_events, "symbol not in universe"), None

    try:
        df = market_data.history(
            market_data.HistoryRequest(symbol=symbol, period="1y"),
            run_id=run_id,
        )
    except Exception as e:
        return _empty_row(entry, symbol, macro_events, f"{type(e).__name__}: {e}"), None

    if df is None or df.empty or "Close" not in df.columns:
        return _empty_row(entry, symbol, macro_events, "no history available"), None

    closes = df["Close"]
    returns = closes.pct_change()
    vols = df["Volume"] if "Volume" in df.columns else pd.Series(dtype=float)
    last_close = float(closes.iloc[-1])
    adv_30d = int(vols.tail(30).mean()) if not vols.empty else None

    mom30 = _trailing_return_pct(closes, 30)
    rel_strength = (
        round(mom30 - spy_return_30d_pct, 2)
        if mom30 is not None and spy_return_30d_pct is not None else None
    )

    row = FeatureRow(
        symbol=symbol,
        kind=entry.kind,
        factor=entry.factor,
        leverage_factor=entry.leverage_factor,
        family=entry.family,
        last_close=round(last_close, 4),
        adv_30d=adv_30d,
        momentum_30d_pct=_round(mom30, 2),
        momentum_60d_pct=_round(_trailing_return_pct(closes, 60), 2),
        hv_30d_annualised=_round(_annualised_vol(returns, 30), 4),
        hv_90d_annualised=_round(_annualised_vol(returns, 90), 4),
        dist_from_50d_ma_pct=_round(_dist_from_ma_pct(closes, 50), 2),
        dist_from_200d_ma_pct=_round(_dist_from_ma_pct(closes, 200), 2),
        rsi_14=_round(_rsi(closes), 1),
        rel_strength_spy_30d=rel_strength,
        trend_r2=_round(_trend_r2(closes), 3),
        upcoming_macro_events_7d=macro_events,
    )
    return row, closes


def _round(v: float | None, ndigits: int) -> float | None:
    return None if v is None else round(v, ndigits)


def _spy_return_30d_pct(*, run_id: str | None) -> float | None:
    """30d SPY total return for the relative-strength column. One extra
    yfinance fetch per cycle; None on any failure (rel strength degrades
    to null, the run continues)."""
    try:
        df = market_data.history(
            market_data.HistoryRequest(symbol="SPY", period="3mo"), run_id=run_id,
        )
        if df is None or df.empty or "Close" not in df.columns:
            return None
        return _trailing_return_pct(df["Close"], 30)
    except Exception:
        return None


def factor_correlations(closes_by_symbol: dict[str, pd.Series]) -> list[dict]:
    """Pairwise 30d daily-return correlation between FACTOR bull ETFs.

    One representative (the highest-|leverage| bull ETF) per factor — the
    inverse leg is its mirror by construction and would only add noise.
    Returns rows {factor_a, factor_b, corr_30d} with |corr| ≥ CORR_MIN_ABS,
    sorted by |corr| descending, capped at CORR_MAX_ROWS so the LLM payload
    stays small.
    """
    # Representative bull symbol per factor.
    rep: dict[str, str] = {}
    rep_mag: dict[str, float] = {}
    for e in universe.UNIVERSE:
        if e.leverage_factor <= 0 or e.symbol not in closes_by_symbol:
            continue
        mag = abs(e.leverage_factor)
        if mag > rep_mag.get(e.factor, -1.0):
            rep[e.factor] = e.symbol
            rep_mag[e.factor] = mag

    rets: dict[str, pd.Series] = {}
    for factor, sym in rep.items():
        closes = closes_by_symbol.get(sym)
        if closes is None:
            continue
        r = closes.pct_change().tail(CORR_LOOKBACK_SESSIONS).dropna()
        if len(r) >= CORR_LOOKBACK_SESSIONS // 2:
            rets[factor] = r

    out: list[dict] = []
    factors = sorted(rets)
    for i, fa in enumerate(factors):
        for fb in factors[i + 1:]:
            joined = pd.concat([rets[fa], rets[fb]], axis=1, join="inner").dropna()
            if len(joined) < CORR_LOOKBACK_SESSIONS // 2:
                continue
            corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
            if corr is None or pd.isna(corr) or abs(corr) < CORR_MIN_ABS:
                continue
            out.append({
                "factor_a": fa, "factor_b": fb, "corr_30d": round(float(corr), 2),
            })
    out.sort(key=lambda r: abs(r["corr_30d"]), reverse=True)
    return out[:CORR_MAX_ROWS]


def compute_signals(*, run_id: str, symbols: Iterable[str] | None = None) -> dict:
    """Compute the v2 signals table. Schema: signals.schema.json.

    By default iterates the full v2 universe. ``symbols`` lets callers
    constrain to a subset (used by tests).
    """
    syms = list(symbols) if symbols is not None else universe.all_symbols()
    spy_ret = _spy_return_30d_pct(run_id=run_id)
    rows: list[FeatureRow] = []
    closes_by_symbol: dict[str, pd.Series] = {}
    for s in syms:
        row, closes = _row_for_symbol(s, run_id=run_id, spy_return_30d_pct=spy_ret)
        rows.append(row)
        if closes is not None:
            closes_by_symbol[s] = closes
    return {
        "run_id": run_id,
        "generated_at": state.utcnow_iso(),
        "tickers": [r.to_dict() for r in rows],
        "factor_correlations": factor_correlations(closes_by_symbol),
    }


# ----- compact LLM rendering -----

# Per-ticker numeric fields inlined into the compact factor rows, in
# (full_name, short_key) form. Order matters only for readability.
_COMPACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("last_close", "close"),
    ("adv_30d", "adv"),
    ("momentum_30d_pct", "mom30"),
    ("momentum_60d_pct", "mom60"),
    ("hv_30d_annualised", "hv30"),
    ("hv_90d_annualised", "hv90"),
    ("dist_from_50d_ma_pct", "d50"),
    ("dist_from_200d_ma_pct", "d200"),
    ("rsi_14", "rsi14"),
    ("rel_strength_spy_30d", "rs_spy30"),
    ("trend_r2", "trend_r2"),
)


def compact_for_llm(signals_out: dict) -> dict:
    """Factor-grouped, null-stripped rendering of the signals table for
    LLM user messages.

    The full per-ticker table is still written to signals.json (artifact
    + dedup fingerprint are unchanged); this view is only what the
    strategist/constructor/critic read. Grouping a factor's bull+bear
    tickers into one row and dropping nulls/static prose cuts the
    biggest input block on the expensive constructor call — that saving
    pays for the wider universe and the track-record memo, keeping
    per-cycle cost at or below the old baseline.

    Shape:
      {"as_of": ..., "factors": [{"factor": "nasdaq",
         "events_7d": ["CPI 2026-06-10 (0d)"],
         "tickers": [{"sym": "TQQQ", "lev": 3.0, "close": ..., ...},
                     {"sym": "SQQQ", "lev": -3.0, ...}]}],
       "factor_correlations": [...]}
    """
    by_factor: dict[str, dict] = {}
    for t in signals_out.get("tickers", []):
        factor = t.get("factor") or "unknown"
        bucket = by_factor.setdefault(factor, {"factor": factor, "tickers": []})
        if t.get("error"):
            bucket["tickers"].append({"sym": t.get("symbol"), "error": t["error"]})
            continue
        row: dict = {"sym": t.get("symbol"), "lev": t.get("leverage_factor")}
        for full, short in _COMPACT_FIELDS:
            v = t.get(full)
            if v is not None:
                row[short] = v
        bucket["tickers"].append(row)
        # Macro events are shared across a factor's tickers — render once.
        if "events_7d" not in bucket:
            evs = [
                f"{e.get('type')} {e.get('date')} ({e.get('days_away')}d)"
                for e in (t.get("upcoming_macro_events_7d") or [])
            ]
            if evs:
                bucket["events_7d"] = evs
    out = {
        "as_of": signals_out.get("generated_at"),
        "factors": list(by_factor.values()),
    }
    corr = signals_out.get("factor_correlations")
    if corr:
        out["factor_correlations"] = corr
    return out
