from __future__ import annotations

import pytest

from lib import risk


def test_size_position_basic():
    plan = risk.size_position(nav_usd=2500, target_pct=10, unit_price_usd=50)
    assert plan.shares_or_contracts == 5
    assert plan.notional_usd == 250
    assert plan.target_pct == pytest.approx(10.0)


def test_size_position_rejects_over_15_pct():
    with pytest.raises(risk.RiskViolation):
        risk.size_position(nav_usd=2500, target_pct=15.5, unit_price_usd=10)


def test_size_position_rejects_unaffordable():
    with pytest.raises(risk.RiskViolation):
        # 1% of $2500 = $25; one share at $1000 won't fit
        risk.size_position(nav_usd=2500, target_pct=1, unit_price_usd=1000)


def test_kill_etf_at_25_pct_loss():
    kill, why = risk.should_kill_position(
        current_value_usd=75, cost_basis_usd=100, is_option=False
    )
    assert kill and "25%" in why


def test_etf_does_not_kill_at_24_pct_loss():
    kill, _ = risk.should_kill_position(
        current_value_usd=76, cost_basis_usd=100, is_option=False
    )
    assert not kill


def test_option_kill_only_at_full_premium_loss():
    # 50% drawdown on a long option does not trip; spec uses 100% for options
    kill, _ = risk.should_kill_position(
        current_value_usd=50, cost_basis_usd=100, is_option=True
    )
    assert not kill
    kill, _ = risk.should_kill_position(
        current_value_usd=0, cost_basis_usd=100, is_option=True
    )
    assert kill


def test_extra_kill_underlying_below():
    kill, why = risk.should_kill_position(
        current_value_usd=90, cost_basis_usd=100, is_option=False,
        extra_kill={"underlying_price_below": 50}, spot_price=49,
    )
    assert kill and "kill_below" in why


def test_circuit_breaker_8pct_drop():
    tripped, dd = risk.daily_circuit_breaker_tripped(sod_nav_usd=2500, current_nav_usd=2299)
    assert tripped
    assert dd >= 8.0
    tripped, _ = risk.daily_circuit_breaker_tripped(sod_nav_usd=2500, current_nav_usd=2350)
    assert not tripped


@pytest.mark.parametrize("count,all_cash,ok", [
    (1, False, True), (2, False, True), (3, False, True),
    (5, False, True), (8, False, True), (10, False, True), (12, False, True),
    (0, False, False), (13, False, False),
    (0, True, True), (1, True, False),
])
def test_position_band(count, all_cash, ok):
    assert risk.position_band_ok(count, all_cash) is ok
