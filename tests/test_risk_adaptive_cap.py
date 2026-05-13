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
