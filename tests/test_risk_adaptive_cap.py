"""Tests for lib/risk.adaptive_position_cap_pct."""
from __future__ import annotations

import pytest

from lib import risk


def test_at_peak_returns_base_cap():
    assert risk.adaptive_position_cap_pct(current_nav=2500, peak_nav_30d=2500) == 15.0


def test_above_peak_still_returns_base_cap():
    """NAV up from prior peak — current is the new peak."""
    assert risk.adaptive_position_cap_pct(current_nav=2700, peak_nav_30d=2500) == 15.0


def test_at_or_below_10_percent_drawdown_returns_floor():
    """≥10% drawdown → halved cap (7.5%)."""
    # 10% drawdown
    assert risk.adaptive_position_cap_pct(current_nav=2250, peak_nav_30d=2500) == 7.5
    # 20% drawdown
    assert risk.adaptive_position_cap_pct(current_nav=2000, peak_nav_30d=2500) == 7.5


def test_5_percent_drawdown_linearly_interpolates():
    """5% drawdown halfway between (0%, 15) and (10%, 7.5).
    Expected cap: 15 - 0.5 × (15 - 7.5) = 11.25.
    """
    cap = risk.adaptive_position_cap_pct(current_nav=2375, peak_nav_30d=2500)
    assert cap == pytest.approx(11.25, abs=0.01)


def test_zero_or_negative_peak_returns_base():
    """Cold-start path (no nav history yet) → base cap."""
    assert risk.adaptive_position_cap_pct(current_nav=2500, peak_nav_30d=0) == 15.0
    assert risk.adaptive_position_cap_pct(current_nav=2500, peak_nav_30d=-10) == 15.0


# ---- adaptive hold ceiling (the drift bound on already-open positions) ----


def test_hold_ceiling_at_peak_returns_base():
    assert risk.adaptive_hold_ceiling_pct(current_nav=2500, peak_nav_30d=2500) == 25.0


def test_hold_ceiling_above_peak_returns_base():
    assert risk.adaptive_hold_ceiling_pct(current_nav=2700, peak_nav_30d=2500) == 25.0


def test_hold_ceiling_at_or_below_10_percent_drawdown_returns_floor():
    """≥10% drawdown → halved ceiling (12.5%)."""
    assert risk.adaptive_hold_ceiling_pct(current_nav=2250, peak_nav_30d=2500) == 12.5
    assert risk.adaptive_hold_ceiling_pct(current_nav=2000, peak_nav_30d=2500) == 12.5


def test_hold_ceiling_5_percent_drawdown_linearly_interpolates():
    """5% drawdown halfway between (0%, 25) and (10%, 12.5) → 18.75."""
    cap = risk.adaptive_hold_ceiling_pct(current_nav=2375, peak_nav_30d=2500)
    assert cap == pytest.approx(18.75, abs=0.01)


def test_hold_ceiling_zero_or_negative_peak_returns_base():
    assert risk.adaptive_hold_ceiling_pct(current_nav=2500, peak_nav_30d=0) == 25.0
    assert risk.adaptive_hold_ceiling_pct(current_nav=2500, peak_nav_30d=-10) == 25.0


def test_hold_ceiling_always_above_entry_cap():
    """The two ceilings ride one curve and never invert."""
    for cur, peak in [(2500, 2500), (2375, 2500), (2250, 2500), (2000, 2500)]:
        entry = risk.adaptive_position_cap_pct(current_nav=cur, peak_nav_30d=peak)
        hold = risk.adaptive_hold_ceiling_pct(current_nav=cur, peak_nav_30d=peak)
        assert hold > entry
