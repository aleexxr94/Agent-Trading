"""Unit tests for lib.benchmark — financial maths + alignment.

No network: fetch_spy_total_return is monkeypatched everywhere it's needed.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from lib import benchmark as bench


# ---------- pure metric helpers ----------


def test_cagr_known_inputs():
    # $100 → $121 over 730 days (≈2 years) → exactly 10% per year.
    assert bench.cagr(100.0, 121.0, 730) == pytest.approx(0.10, abs=1e-3)


def test_cagr_negative_return():
    # $100 → $50 over 730 days → -29.3% per year.
    assert bench.cagr(100.0, 50.0, 730) == pytest.approx(-0.2929, abs=1e-3)


def test_cagr_degenerate_inputs_return_zero():
    assert bench.cagr(0.0, 100.0, 365) == 0.0
    assert bench.cagr(100.0, 0.0, 365) == 0.0
    assert bench.cagr(100.0, 121.0, 0) == 0.0


def test_sharpe_zero_returns_returns_zero():
    s = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0])
    assert bench.sharpe(s) == 0.0


def test_sharpe_constant_positive_returns_sentinel():
    # All same value → std=0; Sharpe must return 0.0 sentinel, not inf/NaN.
    s = pd.Series([0.01, 0.01, 0.01, 0.01, 0.01])
    assert bench.sharpe(s) == 0.0


def test_sharpe_positive_when_returns_trend_up():
    s = pd.Series([0.001, 0.002, 0.001, 0.003, 0.002, 0.001])
    assert bench.sharpe(s) > 0.0


def test_annualised_vol_known_inputs():
    # std of [-0.01, 0.01, -0.01, 0.01] (ddof=1) ≈ 0.01155; × √252 ≈ 0.1833
    s = pd.Series([-0.01, 0.01, -0.01, 0.01])
    assert bench.annualised_vol(s) == pytest.approx(0.1833, abs=1e-3)


def test_annualised_vol_empty_returns_zero():
    assert bench.annualised_vol(pd.Series([], dtype=float)) == 0.0


def test_max_drawdown_known_inputs():
    # [100, 120, 80, 130] — peak=120 (day 1), trough=80 (day 2) → -33.33%
    idx = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
    eq = pd.Series([100.0, 120.0, 80.0, 130.0], index=idx)
    dd_pct, peak, trough = bench.max_drawdown(eq)
    assert dd_pct == pytest.approx(-1.0 / 3.0, abs=1e-4)
    assert peak == date(2026, 1, 2)
    assert trough == date(2026, 1, 3)


def test_max_drawdown_monotonic_series_returns_zero():
    idx = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    eq = pd.Series([100.0, 110.0, 120.0], index=idx)
    dd_pct, _, _ = bench.max_drawdown(eq)
    assert dd_pct == 0.0


def test_correlation_perfect_positive():
    a = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    b = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0])
    assert bench.correlation(a, b) == pytest.approx(1.0, abs=1e-9)


def test_correlation_negative():
    a = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    b = pd.Series([-1.0, -2.0, -3.0, -4.0, -5.0])
    assert bench.correlation(a, b) == pytest.approx(-1.0, abs=1e-9)


def test_correlation_zero_std_returns_zero():
    a = pd.Series([1.0, 1.0, 1.0, 1.0])
    b = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert bench.correlation(a, b) == 0.0


def test_correlation_label_buckets():
    assert bench.correlation_label(0.0) == "Low"
    assert bench.correlation_label(0.29) == "Low"
    assert bench.correlation_label(0.3) == "Moderate"
    assert bench.correlation_label(0.7) == "Moderate"
    assert bench.correlation_label(0.71) == "High"
    assert bench.correlation_label(-0.85) == "High"


# ---------- alignment ----------


def test_align_to_eod_downsamples_to_last_cycle():
    rows = [
        {"at": "2026-05-25T13:00:00Z", "nav_usd": 2500.0},
        {"at": "2026-05-25T17:00:00Z", "nav_usd": 2510.0},
        {"at": "2026-05-25T21:00:00Z", "nav_usd": 2520.0},  # ← last on the 25th
        {"at": "2026-05-26T13:00:00Z", "nav_usd": 2530.0},
    ]
    df = bench.align_to_eod(rows)
    assert list(df.index) == [date(2026, 5, 25), date(2026, 5, 26)]
    assert df.loc[date(2026, 5, 25), "nav"] == 2520.0
    assert df.loc[date(2026, 5, 26), "nav"] == 2530.0


def test_align_to_eod_handles_empty():
    df = bench.align_to_eod([])
    assert df.empty
    assert list(df.columns) == ["nav"]


def test_align_to_eod_skips_unparseable_rows():
    rows = [
        {"at": "garbage", "nav_usd": 2500.0},
        {"at": "2026-05-25T13:00:00Z", "nav_usd": "not-a-number"},
        {"at": "2026-05-25T17:00:00Z", "nav_usd": 2510.0},
    ]
    df = bench.align_to_eod(rows)
    assert len(df) == 1
    assert df.iloc[0]["nav"] == 2510.0


# ---------- monthly returns ----------


def _daily_series(start: date, vals: list[float]) -> pd.Series:
    idx = pd.bdate_range(start=start, periods=len(vals))
    return pd.Series(vals, index=idx)


def test_monthly_returns_simple():
    # Two calendar months: Jan ends at 110 (from 100 start → +10%);
    # Feb ends at 121 (110 → +10%).
    idx = [
        date(2026, 1, 5), date(2026, 1, 15), date(2026, 1, 30),
        date(2026, 2, 2), date(2026, 2, 15), date(2026, 2, 27),
    ]
    eq = pd.Series([100.0, 105.0, 110.0, 112.0, 118.0, 121.0], index=idx)
    out = bench.monthly_returns(eq, as_of=date(2026, 2, 28))
    assert list(out["month"]) == ["2026-01", "2026-02"]
    assert out["ret_pct"].iloc[0] == pytest.approx(10.0, abs=1e-6)
    assert out["ret_pct"].iloc[1] == pytest.approx(10.0, abs=1e-6)
    assert out["is_partial"].iloc[0] is False or out["is_partial"].iloc[0] == False  # noqa: E712
    assert out["is_partial"].iloc[1] is False or out["is_partial"].iloc[1] == False  # noqa: E712


def test_monthly_returns_partial_current_month():
    idx = [date(2026, 1, 31), date(2026, 2, 1), date(2026, 2, 10)]
    eq = pd.Series([100.0, 102.0, 105.0], index=idx)
    out = bench.monthly_returns(eq, as_of=date(2026, 2, 10))
    # Feb 2026 not complete yet.
    feb = out[out["month"] == "2026-02"].iloc[0]
    assert feb["is_partial"] == True  # noqa: E712


def test_monthly_returns_late_inception_flags_first_month_partial():
    # Jan 31 inception + Feb 1 observation. The Jan bucket has only a
    # single-day stub of data — counting it as a completed month would
    # poison the % months beat SPY metric (regression for codex P2).
    idx = [date(2026, 1, 31), date(2026, 2, 1)]
    eq = pd.Series([100.0, 100.5], index=idx)
    out = bench.monthly_returns(eq, as_of=date(2026, 2, 1))
    jan = out[out["month"] == "2026-01"].iloc[0]
    assert jan["is_partial"] == True  # noqa: E712


def test_monthly_returns_early_inception_keeps_first_month_complete():
    # Inception at the start of January (Jan 2 = first trading day in 2026).
    # The first month should still count as completed once February is full.
    idx = [date(2026, 1, 2), date(2026, 1, 30), date(2026, 2, 27)]
    eq = pd.Series([100.0, 105.0, 110.0], index=idx)
    out = bench.monthly_returns(eq, as_of=date(2026, 3, 1))
    jan = out[out["month"] == "2026-01"].iloc[0]
    assert jan["is_partial"] == False  # noqa: E712


# ---------- build_comparison ----------


def _strategy_eod(start: date, vals: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=len(vals))
    return pd.DataFrame({"nav": vals}, index=[d.date() for d in idx])


def _spy_df(start: date, closes: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame({"close": closes}, index=[d.date() for d in idx])


def test_build_comparison_basic():
    start = date(2026, 1, 5)
    strat = _strategy_eod(start, [2500.0, 2520.0, 2510.0, 2540.0, 2560.0])
    spy = _spy_df(start, [400.0, 402.0, 401.0, 403.0, 405.0])
    bundle = bench.build_comparison(strat, spy, 2500.0, as_of=date(2026, 1, 9))
    assert bundle is not None
    # Strategy total return: 2560/2500 - 1 = +2.4%
    assert bundle.strategy_total_return_pct == pytest.approx(2.4, abs=1e-6)
    # SPY total return: 405/400 - 1 = +1.25%
    assert bundle.spy_total_return_pct == pytest.approx(1.25, abs=1e-6)
    # SPY-equivalent ends at 2500 * 405/400 = 2531.25
    assert float(bundle.spy_curve["nav"].iloc[-1]) == pytest.approx(2531.25, abs=1e-6)
    assert bundle.delta_usd == pytest.approx(2560.0 - 2531.25, abs=1e-6)


def test_build_comparison_strategy_return_anchored_at_starting_balance():
    # First aligned NAV sample is NOT starting_balance_usd — e.g. the
    # first logged cycle already includes costs/fills. The strategy
    # total return and CAGR must still be computed against
    # starting_balance_usd so they're symmetric with the SPY anchor
    # (regression for codex P2).
    start = date(2026, 1, 5)
    strat = _strategy_eod(start, [2498.0, 2510.0, 2520.0, 2540.0, 2575.0])
    spy = _spy_df(start, [400.0, 402.0, 404.0, 405.0, 410.0])
    bundle = bench.build_comparison(strat, spy, 2500.0, as_of=date(2026, 1, 9))
    # Strategy total return: 2575 / 2500 - 1 = +3.00% (NOT 2575/2498 ≈ +3.08%).
    assert bundle.strategy_total_return_pct == pytest.approx(3.0, abs=1e-6)
    # SPY: anchored at $2,500 by construction → 2562.50 / 2500 - 1 = +2.50%.
    assert bundle.spy_total_return_pct == pytest.approx(2.5, abs=1e-6)
    # Dollar delta is independent of denominator choice.
    assert bundle.delta_usd == pytest.approx(2575.0 - 2562.5, abs=1e-6)


def test_build_comparison_cagr_anchored_at_starting_balance():
    # 120 days of trading data with a first-cycle dip below the baseline.
    # CAGR must use the starting balance, not the first observed NAV.
    start = date(2026, 1, 5)
    days = 120
    idx = pd.bdate_range(start=start, periods=days)
    # Strategy starts at $2,490 (already down 0.4%) and ends at $2,800.
    strat = pd.DataFrame(
        {"nav": [2490.0] + [2490.0 + i * 2.6 for i in range(1, days)]},
        index=[d.date() for d in idx],
    )
    spy = pd.DataFrame(
        {"close": [400.0 + i * 0.1 for i in range(days)]},
        index=[d.date() for d in idx],
    )
    bundle = bench.build_comparison(strat, spy, 2500.0, as_of=idx[-1].date())
    # CAGR must reflect (final / 2500), not (final / 2490).
    final_nav = float(strat["nav"].iloc[-1])
    span_days = (idx[-1].date() - idx[0].date()).days
    expected = bench.cagr(2500.0, final_nav, span_days)
    assert bundle.cagr_strategy == pytest.approx(expected, abs=1e-9)


def test_build_comparison_empty_spy_returns_none():
    start = date(2026, 1, 5)
    strat = _strategy_eod(start, [2500.0, 2520.0])
    empty_spy = pd.DataFrame(columns=["close"]).rename_axis("date")
    assert bench.build_comparison(strat, empty_spy, 2500.0) is None


def test_build_comparison_too_few_rows_returns_none():
    start = date(2026, 1, 5)
    strat = _strategy_eod(start, [2500.0])
    spy = _spy_df(start, [400.0])
    assert bench.build_comparison(strat, spy, 2500.0) is None


def test_build_comparison_weekend_inception_aligns_to_first_shared_trading_day():
    # Strategy "inception" recorded on a Saturday but the first trading day
    # also has a NAV sample. Inner-join discards the Saturday since SPY has
    # no row for that date, and anchors on Monday's SPY close.
    saturday = date(2026, 5, 23)
    monday = date(2026, 5, 25)
    tuesday = date(2026, 5, 26)
    strat = pd.DataFrame(
        {"nav": [2500.0, 2500.0, 2510.0]},
        index=[saturday, monday, tuesday],
    )
    spy = pd.DataFrame(
        {"close": [400.0, 401.0]},
        index=[monday, tuesday],
    )
    bundle = bench.build_comparison(strat, spy, 2500.0, as_of=tuesday)
    assert bundle is not None
    # Inception = first joined trading day = Monday.
    assert bundle.inception == monday
    # SPY-equivalent anchored at Monday's close.
    assert float(bundle.spy_curve["nav"].iloc[0]) == pytest.approx(2500.0, abs=1e-6)


def test_build_comparison_live_tip_extends_strategy_curve():
    start = date(2026, 1, 5)
    strat = _strategy_eod(start, [2500.0, 2520.0, 2510.0])
    spy = _spy_df(start, [400.0, 402.0, 401.0])
    as_of = date(2026, 1, 9)  # later than the last joined day (2026-01-07)
    bundle = bench.build_comparison(
        strat, spy, 2500.0, live_nav_usd=2555.0, as_of=as_of,
    )
    assert bundle is not None
    # Strategy curve now extends to the as_of date with the live NAV.
    assert bundle.strategy_curve.index[-1] == as_of
    assert float(bundle.strategy_curve["nav"].iloc[-1]) == 2555.0
    # SPY-equivalent extends with the last known close so the two series
    # remain co-terminal on the x-axis.
    assert bundle.spy_curve.index[-1] == as_of


def test_build_comparison_cagr_suppressed_under_90_days():
    start = date(2026, 1, 5)
    strat = _strategy_eod(start, [2500.0 + i for i in range(10)])
    spy = _spy_df(start, [400.0 + i * 0.1 for i in range(10)])
    bundle = bench.build_comparison(strat, spy, 2500.0)
    assert bundle is not None
    assert bundle.cagr_strategy is None
    assert bundle.cagr_spy is None


def test_build_comparison_cagr_populated_when_span_exceeds_90_days():
    start = date(2026, 1, 5)
    days = 120
    idx = pd.bdate_range(start=start, periods=days)
    strat = pd.DataFrame({"nav": [2500.0 + i for i in range(days)]}, index=[d.date() for d in idx])
    spy = pd.DataFrame({"close": [400.0 + i * 0.05 for i in range(days)]}, index=[d.date() for d in idx])
    bundle = bench.build_comparison(strat, spy, 2500.0, as_of=idx[-1].date())
    assert bundle is not None
    assert bundle.cagr_strategy is not None
    assert bundle.cagr_spy is not None


def test_total_return_history_passes_end_plus_one_day_to_yfinance(monkeypatch):
    """yfinance treats history(end=...) as EXCLUSIVE. The wrapper must
    pass end+1 day so the caller's inclusive end date is actually
    returned. Regression for codex P2: without this, a freshly-installed
    dashboard with 2 trading days of NAV history would get only 1 SPY
    close back and stay stuck in the empty state.
    """
    from datetime import date as _date

    captured: dict = {}

    class _FakeTicker:
        def __init__(self, symbol):
            captured["symbol"] = symbol

        def history(self, *, start, end, auto_adjust):
            captured["start"] = start
            captured["end"] = end
            captured["auto_adjust"] = auto_adjust
            import pandas as _pd
            return _pd.DataFrame()

    class _FakeYf:
        def Ticker(self, symbol):
            return _FakeTicker(symbol)

    from lib import market_data
    monkeypatch.setattr(market_data, "_yf", lambda: _FakeYf())

    market_data.total_return_history(
        "SPY", start=_date(2026, 1, 5), end=_date(2026, 1, 10),
    )

    assert captured["symbol"] == "SPY"
    assert captured["start"] == "2026-01-05"
    assert captured["end"] == "2026-01-11"  # end + 1 day
    assert captured["auto_adjust"] is True


def test_build_comparison_months_table_populated_after_one_month():
    # Two full months of trading data.
    start = date(2026, 1, 2)
    end = date(2026, 2, 27)
    idx = pd.bdate_range(start=start, end=end)
    n = len(idx)
    strat = pd.DataFrame({"nav": [2500.0 + i * 2.0 for i in range(n)]}, index=[d.date() for d in idx])
    spy = pd.DataFrame({"close": [400.0 + i * 0.1 for i in range(n)]}, index=[d.date() for d in idx])
    bundle = bench.build_comparison(strat, spy, 2500.0, as_of=end)
    assert bundle is not None
    assert bundle.months_table is not None
    assert not bundle.months_table.empty
    # pct_months_strategy_beat is a real number when ≥1 month is present.
    assert bundle.pct_months_strategy_beat is not None
