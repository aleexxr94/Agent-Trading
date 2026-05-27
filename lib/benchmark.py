"""S&P 500 benchmark comparison — pure financial-math helpers.

Computes side-by-side metrics for the live strategy vs a buy-and-hold
position in SPY (total return, dividends reinvested) since the system
went into paper trading.

Alignment policy
----------------
Strategy NAV is sampled multiple times per UTC trading day (one per
orchestrator cycle, ~every 4h on weekdays). For comparison against
daily SPY closes the strategy series is **downsampled to the last
cycle of each UTC date**. The two series are then **inner-joined** on
trading date — weekend/holiday strategy samples are dropped.

Hypothetical SPY notional is anchored at the strategy's inception:
`starting_balance_usd` is assumed to buy SPY at the close of the
first trading day on or before the inception cycle. yfinance returns
its index in trading days, so the inner join handles the
weekend-inception case naturally (the first joined row is the next
trading day's close).

Currency: both sides are USD-denominated; no FX adjustment.
Risk-free rate: defaults to 0 — surfaced honestly in the tooltip.

Assumes a constant starting balance — no deposits/withdrawals are
tracked by the system today. If that ever changes this comparison
becomes unfair and a money-weighted (IRR) variant is needed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class MetricsBundle:
    strategy_curve: "pd.DataFrame"          # index=date, col='nav'
    spy_curve: "pd.DataFrame"               # index=date, col='nav'  (hypothetical $X in SPY)
    strategy_total_return_pct: float
    spy_total_return_pct: float
    delta_usd: float
    delta_pct: float
    cagr_strategy: float | None             # None when span < 90 days
    cagr_spy: float | None
    sharpe_strategy: float                  # rf=0, annualised on trading-day returns
    sharpe_spy: float
    vol_strategy_ann: float
    vol_spy_ann: float
    max_dd_strategy: tuple[float, date, date]   # (pct, peak_date, trough_date)
    max_dd_spy: tuple[float, date, date]
    correlation: float                      # Pearson on daily returns
    correlation_label_text: str             # "Low" | "Moderate" | "High"
    pct_months_strategy_beat: float | None  # None when < 1 complete month
    months_table: "pd.DataFrame | None"     # cols: month, strat_ret_pct, spy_ret_pct, delta_pct, strat_eom, spy_eom, is_partial
    inception: date
    as_of: date
    starting_balance_usd: float


# ---------- alignment helpers ----------


def _parse_iso_utc(ts: str) -> datetime:
    """Tolerate trailing 'Z' and missing tzinfo. Returns aware UTC."""
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def align_to_eod(
    nav_rows: list[dict],
    *,
    value_key: str = "nav_usd",
) -> "pd.DataFrame":
    """Downsample per-event strategy values to one row per UTC trading day.

    Takes the LAST sample of each date (latest `at` wins). Returns a
    DataFrame indexed by `date` with a single column `nav`. Rows whose
    `at` or value can't be parsed are skipped.

    ``value_key`` is the field name to read for each row's value —
    defaults to ``nav_usd`` for backward compatibility with
    ``state/nav_history.jsonl``. The benchmark tab passes the realised-
    P&L key (``synthetic_realized_balance_usd``) so the strategy curve
    reflects actual P&L rather than sizing notional.
    """
    import pandas as pd  # noqa: WPS433

    parsed: list[tuple[date, datetime, float]] = []
    for row in nav_rows or []:
        at = row.get("at")
        val = row.get(value_key)
        if at is None or not isinstance(val, (int, float)):
            continue
        try:
            dt = _parse_iso_utc(str(at))
        except (ValueError, TypeError):
            continue
        parsed.append((dt.date(), dt, float(val)))

    if not parsed:
        return pd.DataFrame(columns=["nav"]).rename_axis("date")

    df = pd.DataFrame(parsed, columns=["date", "at", "nav"])
    df = df.sort_values("at").groupby("date", as_index=True).tail(1)
    df = df.set_index("date")[["nav"]].sort_index()
    return df


def fetch_spy_total_return(start: date, end: date) -> "pd.DataFrame":
    """Total-return SPY closes (dividends reinvested) between start and end.

    Delegates to lib.market_data.total_return_history so the network/cache
    behaviour lives next to the rest of the yfinance code. Returns a
    DataFrame indexed by `date` with a single column `close`.
    """
    import pandas as pd  # noqa: WPS433

    from . import market_data

    raw = market_data.total_return_history("SPY", start=start, end=end)
    if raw is None or getattr(raw, "empty", True):
        return pd.DataFrame(columns=["close"]).rename_axis("date")

    out = raw.copy()
    if "Close" in out.columns:
        out = out[["Close"]].rename(columns={"Close": "close"})
    elif "close" not in out.columns:
        return pd.DataFrame(columns=["close"]).rename_axis("date")
    out.index = pd.to_datetime(out.index, utc=True).date
    out.index.name = "date"
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


# ---------- metric helpers ----------


def cagr(start_val: float, end_val: float, days: int) -> float:
    """Compound annual growth rate.

    (end/start)^(365/days) - 1. Works for negative total returns as long
    as both values are positive.

    Special-cases a wiped-out account: if end_val <= 0 we return -1.0
    (= -100% annualised) so the dashboard's CAGR card shows -100%
    instead of a misleading flat 0% — relevant for this options-heavy
    $2,500 experiment where losses + logged costs could in principle
    consume the baseline (regression for codex P2). Returns 0.0 only
    for clearly invalid inputs (non-positive start_val, non-positive
    days) which shouldn't occur in practice given the >=90-day gate
    upstream.
    """
    if days <= 0 or start_val <= 0:
        return 0.0
    if end_val <= 0:
        return -1.0
    return (end_val / start_val) ** (365.0 / float(days)) - 1.0


def sharpe(
    daily_returns: "pd.Series",
    rf: float = 0.0,
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualised Sharpe on daily returns.

    rf is the annual risk-free rate (default 0). std=0 → returns 0.0 as
    a sentinel rather than inf/NaN so the dashboard doesn't render gibberish.
    """
    if daily_returns is None or len(daily_returns) < 2:
        return 0.0
    rf_per_period = float(rf) / float(periods_per_year)
    excess = daily_returns.dropna().astype(float) - rf_per_period
    if len(excess) < 2:
        return 0.0
    std = float(excess.std(ddof=1))
    if std == 0.0 or math.isnan(std):
        return 0.0
    return float(excess.mean()) / std * math.sqrt(periods_per_year)


def annualised_vol(
    daily_returns: "pd.Series",
    periods_per_year: int = _TRADING_DAYS_PER_YEAR,
) -> float:
    if daily_returns is None or len(daily_returns) < 2:
        return 0.0
    std = float(daily_returns.dropna().astype(float).std(ddof=1))
    if math.isnan(std):
        return 0.0
    return std * math.sqrt(periods_per_year)


def max_drawdown(equity: "pd.Series") -> tuple[float, date, date]:
    """Largest peak-to-trough decline. Returns (pct, peak_date, trough_date).

    pct is negative (-0.25 = 25% drawdown). When the series is empty or
    monotonically non-decreasing, returns (0.0, today, today).
    """
    if equity is None or len(equity) == 0:
        today = date.today()
        return (0.0, today, today)
    eq = equity.dropna().astype(float)
    if len(eq) == 0:
        today = date.today()
        return (0.0, today, today)
    running_peak = eq.cummax()
    drawdowns = eq / running_peak - 1.0
    trough_pos = drawdowns.idxmin()
    trough_val = float(drawdowns.loc[trough_pos])
    peak_pos = eq.loc[:trough_pos].idxmax()
    return (trough_val, _to_date(peak_pos), _to_date(trough_pos))


def _to_date(idx_val) -> date:
    if isinstance(idx_val, date) and not isinstance(idx_val, datetime):
        return idx_val
    if isinstance(idx_val, datetime):
        return idx_val.date()
    import pandas as pd  # noqa: WPS433
    return pd.Timestamp(idx_val).date()


def correlation(a: "pd.Series", b: "pd.Series") -> float:
    """Pearson correlation. NaN-safe: returns 0.0 when undefined."""
    if a is None or b is None or len(a) < 2 or len(b) < 2:
        return 0.0
    import pandas as pd  # noqa: WPS433
    aa = pd.Series(a).astype(float).reset_index(drop=True)
    bb = pd.Series(b).astype(float).reset_index(drop=True)
    n = min(len(aa), len(bb))
    aa, bb = aa.iloc[:n], bb.iloc[:n]
    if aa.std(ddof=1) == 0 or bb.std(ddof=1) == 0:
        return 0.0
    r = float(aa.corr(bb))
    if math.isnan(r):
        return 0.0
    return r


def correlation_label(r: float) -> str:
    a = abs(float(r))
    if a < 0.3:
        return "Low"
    if a <= 0.7:
        return "Moderate"
    return "High"


def monthly_returns(
    equity: "pd.Series",
    *,
    as_of: date | None = None,
    baseline: float | None = None,
) -> "pd.DataFrame":
    """End-of-month equity values and month-over-month returns.

    Returns a DataFrame with columns:
      month       : 'YYYY-MM' string
      ret_pct     : month-over-month return as percent (12.34 = +12.34%)
      eom_value   : last equity value in that month
      is_partial  : True for the most recent month when as_of is before
                    that month's end, OR for the first month when
                    inception was after day 5 of that month.

    ``baseline`` overrides the first month's denominator. The benchmark
    tab passes ``starting_balance_usd`` so the monthly table and the
    headline metrics share an anchor — without this, a first observed
    NAV of $2,400 followed by $2,500 EoM would report January as
    +4.17% even though the fixed-baseline experiment return is 0%.
    Defaults to ``equity.iloc[0]`` for backward compatibility.
    """
    import pandas as pd  # noqa: WPS433

    if equity is None or len(equity) == 0:
        return pd.DataFrame(columns=["month", "ret_pct", "eom_value", "is_partial"])
    eq = equity.dropna().astype(float)
    if len(eq) == 0:
        return pd.DataFrame(columns=["month", "ret_pct", "eom_value", "is_partial"])

    df = pd.DataFrame({"nav": eq.values}, index=pd.to_datetime(eq.index))
    df["month"] = df.index.to_period("M").astype(str)
    eom = df.groupby("month", as_index=False).agg(eom_value=("nav", "last"))

    start_val = float(baseline) if baseline is not None else float(eq.iloc[0])
    prevs = [start_val] + eom["eom_value"].tolist()[:-1]
    eom["ret_pct"] = [
        (cur / prev - 1.0) * 100.0 if prev > 0 else 0.0
        for cur, prev in zip(eom["eom_value"].tolist(), prevs)
    ]

    last_date = eq.index[-1]
    if not isinstance(last_date, date) or isinstance(last_date, datetime):
        last_date = _to_date(last_date)
    first_date = eq.index[0]
    if not isinstance(first_date, date) or isinstance(first_date, datetime):
        first_date = _to_date(first_date)
    reference = as_of or last_date
    is_partial_flags = [False] * len(eom)

    # Last bucket: partial if as_of hasn't reached month-end.
    last_month = eom["month"].iloc[-1]
    last_period_end = pd.Period(last_month, freq="M").end_time.date()
    if reference < last_period_end:
        is_partial_flags[-1] = True

    # First bucket: partial unless inception was at/near the month start.
    # A late-month inception (e.g. Jan 31) leaves us with only a tiny
    # stub of January — counting that as a "completed month" would
    # poison the `% months beat SPY` metric. Threshold of 5 calendar
    # days covers weekend/holiday-affected month starts (e.g. Jan 1
    # 2026 was a Thursday and a market holiday; first trading day was
    # Jan 2) without being too strict.
    if first_date.day > 5:
        is_partial_flags[0] = True

    eom["is_partial"] = is_partial_flags
    return eom[["month", "ret_pct", "eom_value", "is_partial"]]


# ---------- comparison assembly ----------


def build_comparison(
    strategy_eod: "pd.DataFrame",
    spy: "pd.DataFrame",
    starting_balance_usd: float,
    *,
    live_nav_usd: float | None = None,
    as_of: date | None = None,
) -> MetricsBundle | None:
    """Assemble the side-by-side metrics bundle.

    Inner-joins strategy EOD NAV with SPY closes on date. Builds a
    hypothetical SPY-equivalent equity curve as
    `starting_balance_usd * close / close.iloc[0]`. Optionally appends
    a live tip row (today's broker equity for the strategy + latest SPY
    close) so the dashboard's "current value" matches what users see on
    the Performance tab.

    Returns None when fewer than 2 aligned trading days exist — the UI
    treats this as the empty-state placeholder.
    """
    import pandas as pd  # noqa: WPS433

    if strategy_eod is None or spy is None:
        return None
    if getattr(strategy_eod, "empty", True) or getattr(spy, "empty", True):
        return None

    # Forward-fill the strategy curve across SPY's trading-day index.
    # realized_balance_series only emits rows on event days (close/cost/
    # fee); during a multi-week hold with no events the raw series has
    # one or two points. Inner-joining sparse strategy against dense SPY
    # would then compute "one big multi-week return" for the strategy
    # while SPY has daily returns — Sharpe/vol/correlation become
    # incomparable (regression for codex P2). After ffill, quiet trading
    # days correctly show a 0% strategy return for that day.
    #
    # Use the UNION of strategy and SPY indexes before ffill, then
    # select SPY dates. Reindexing directly to spy_in_range.index would
    # discard a weekend/holiday inception row before ffill could carry
    # it to the next SPY trading day — e.g. Saturday $2,500 inception
    # + Tuesday realized event with SPY rows Mon/Tue would lose
    # Saturday and leave Monday as NaN → dropped → joined len 1 →
    # benchmark returns None (regression for codex P2).
    spy_in_range = spy[spy.index >= strategy_eod.index[0]]
    if spy_in_range.empty:
        return None
    union_idx = strategy_eod.index.union(spy_in_range.index)
    strat_union = strategy_eod.reindex(union_idx).ffill()
    strat_dense = strat_union.loc[spy_in_range.index].dropna()
    joined = strat_dense.join(spy_in_range, how="inner")
    if len(joined) < 2:
        return None

    spy_anchor = float(joined["close"].iloc[0])
    if spy_anchor <= 0:
        return None
    spy_equiv = starting_balance_usd * joined["close"].astype(float) / spy_anchor

    strat = joined["nav"].astype(float).copy()
    spy_curve = spy_equiv.copy()

    today = as_of or date.today()
    if live_nav_usd is not None:
        # Only None means "no live value, skip". Zero or negative are
        # valid live states for a wiped-out account and MUST be
        # surfaced — otherwise the headline cards would still show
        # the last positive historical point as "current" while the
        # account is actually $0 (regression for codex P2). The CAGR
        # helper already handles end_val <= 0 explicitly.
        if today not in strat.index:
            strat.loc[today] = float(live_nav_usd)
            # SPY live tip: use the most recent SPY close at or before
            # `today` from the raw input frame. spy_curve here is the
            # inner-joined version which is truncated to dates where the
            # strategy also had a sample — if today's SPY close exists
            # but the orchestrator hasn't written a same-day NAV row,
            # carrying spy_curve.iloc[-1] forward would compare today's
            # live strategy value against yesterday's SPY close
            # (regression for codex P2).
            spy_idx_le_today = spy.index[spy.index <= today]
            if len(spy_idx_le_today) > 0:
                latest_spy_close = float(spy.loc[spy_idx_le_today[-1], "close"])
                spy_curve.loc[today] = (
                    float(starting_balance_usd) * latest_spy_close / spy_anchor
                )
            else:
                spy_curve.loc[today] = float(spy_curve.iloc[-1])
            strat = strat.sort_index()
            spy_curve = spy_curve.sort_index()
        else:
            strat.loc[today] = float(live_nav_usd)

    inception = _to_date(joined.index[0])
    as_of_final = _to_date(strat.index[-1])
    span_days = max(1, (as_of_final - inception).days)

    # SPY is anchored at the configured starting balance by construction
    # (above). For symmetry — and so the dashboard caption's "fixed $2,500
    # starting balance" promise actually holds — the strategy's total
    # return and CAGR are also computed against starting_balance_usd
    # rather than the first observed NAV sample. The first cycle may
    # already reflect fees/fills/lag and using it as the denominator
    # would silently erase the strategy's day-one P&L (regression for
    # codex P2).
    strat_baseline = float(starting_balance_usd)
    strat_end = float(strat.iloc[-1])
    spy_end = float(spy_curve.iloc[-1])

    strat_total_pct = (strat_end / strat_baseline - 1.0) * 100.0
    spy_total_pct = (spy_end / strat_baseline - 1.0) * 100.0
    delta_usd = strat_end - spy_end
    delta_pct = strat_total_pct - spy_total_pct

    cagr_strat = cagr(strat_baseline, strat_end, span_days) if span_days >= 90 else None
    cagr_spy_v = cagr(strat_baseline, spy_end, span_days) if span_days >= 90 else None

    strat_daily = strat.pct_change().dropna()
    spy_daily = spy_curve.pct_change().dropna()

    sharpe_strat = sharpe(strat_daily)
    sharpe_spy = sharpe(spy_daily)
    vol_strat = annualised_vol(strat_daily)
    vol_spy = annualised_vol(spy_daily)

    dd_strat = max_drawdown(strat)
    dd_spy = max_drawdown(spy_curve)

    n = min(len(strat_daily), len(spy_daily))
    corr = correlation(strat_daily.iloc[-n:], spy_daily.iloc[-n:]) if n >= 2 else 0.0
    corr_lbl = correlation_label(corr)

    months_table: "pd.DataFrame | None"
    pct_beat: float | None
    span_complete_months = (as_of_final.year - inception.year) * 12 + (as_of_final.month - inception.month)
    if span_complete_months < 1:
        months_table = None
        pct_beat = None
    else:
        # Pass `baseline=starting_balance_usd` so the first month's
        # return is anchored at the configured baseline, same as the
        # headline total-return / CAGR calculations above. Without
        # this, a first observed NAV of $2,400 followed by $2,500 EoM
        # would report January as +4.17% and could count as beating
        # SPY in the % months metric, despite the fixed-$2,500
        # experiment return for that month being 0%.
        strat_months = monthly_returns(
            strat, as_of=as_of_final, baseline=strat_baseline,
        )
        # SPY curve is already anchored at strat_baseline by
        # construction, so passing it explicitly is a no-op — but
        # symmetric and safer if the construction ever changes.
        spy_months = monthly_returns(
            spy_curve, as_of=as_of_final, baseline=strat_baseline,
        )
        merged = strat_months.merge(
            spy_months[["month", "ret_pct", "eom_value"]],
            on="month",
            suffixes=("", "_spy"),
        )
        merged = merged.rename(
            columns={
                "ret_pct": "strat_ret_pct",
                "eom_value": "strat_eom",
                "ret_pct_spy": "spy_ret_pct",
                "eom_value_spy": "spy_eom",
            }
        )
        merged["delta_pct"] = merged["strat_ret_pct"] - merged["spy_ret_pct"]
        months_table = merged[
            ["month", "strat_ret_pct", "spy_ret_pct", "delta_pct", "strat_eom", "spy_eom", "is_partial"]
        ]
        complete = months_table[~months_table["is_partial"]]
        if len(complete) == 0:
            pct_beat = None
        else:
            beats = int((complete["delta_pct"] > 0).sum())
            pct_beat = 100.0 * beats / float(len(complete))

    strat_out = strat.rename("nav").to_frame()
    spy_out = spy_curve.rename("nav").to_frame()

    return MetricsBundle(
        strategy_curve=strat_out,
        spy_curve=spy_out,
        strategy_total_return_pct=strat_total_pct,
        spy_total_return_pct=spy_total_pct,
        delta_usd=delta_usd,
        delta_pct=delta_pct,
        cagr_strategy=cagr_strat,
        cagr_spy=cagr_spy_v,
        sharpe_strategy=sharpe_strat,
        sharpe_spy=sharpe_spy,
        vol_strategy_ann=vol_strat,
        vol_spy_ann=vol_spy,
        max_dd_strategy=dd_strat,
        max_dd_spy=dd_spy,
        correlation=corr,
        correlation_label_text=corr_lbl,
        pct_months_strategy_beat=pct_beat,
        months_table=months_table,
        inception=inception,
        as_of=as_of_final,
        starting_balance_usd=float(starting_balance_usd),
    )
