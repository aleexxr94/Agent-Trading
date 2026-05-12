"""Risk primitives — sizing, kill conditions, circuit breakers.

Shared by orchestrator.py (sizing + cap checks at construction time) and
monitor.py (kill-condition evaluation between runs).

Spec invariants:
  - Per-position cap at entry: ≤15% of portfolio NAV.
  - Per-position kill: ≤25% loss of position NAV (or 100% premium for long options).
  - Daily portfolio drawdown circuit breaker: ≥8% in a single UTC day halts new orders.
  - Target band 3–12 positions (or all-cash) — judgement call by the agent.
"""
from __future__ import annotations

from dataclasses import dataclass

# Hard, repo-wide constants — do not parameterise without a CLAUDE.md change.
MAX_POSITION_PCT = 15.0
MAX_POSITION_LOSS_PCT = 25.0
MAX_OPTION_LOSS_PCT = 100.0
DAILY_DD_HALT_PCT = 8.0
TARGET_POSITION_BAND = (3, 12)


class RiskViolation(ValueError):
    """Raised when a sizing or risk rule is breached."""


@dataclass(frozen=True)
class SizingPlan:
    symbol: str
    target_pct: float
    notional_usd: float
    shares_or_contracts: float


def size_position(
    *,
    nav_usd: float,
    target_pct: float,
    unit_price_usd: float,
    is_option: bool = False,
) -> SizingPlan:
    """Return integer shares (ETF) or contracts (option) under the 15% cap.

    For options, unit_price_usd is the per-contract premium in dollars (already
    accounts for the 100x multiplier).
    """
    if nav_usd <= 0:
        raise RiskViolation("NAV must be positive")
    if unit_price_usd <= 0:
        raise RiskViolation("unit price must be positive")
    if target_pct <= 0 or target_pct > MAX_POSITION_PCT:
        raise RiskViolation(
            f"target_pct {target_pct} outside (0, {MAX_POSITION_PCT}]"
        )
    notional = nav_usd * target_pct / 100.0
    raw = notional / unit_price_usd
    units = int(raw)  # always round down
    if units <= 0:
        raise RiskViolation(
            f"insufficient NAV for one unit (raw={raw:.3f}); consider abstaining"
        )
    actual_notional = units * unit_price_usd
    return SizingPlan(
        symbol="",
        target_pct=actual_notional / nav_usd * 100.0,
        notional_usd=actual_notional,
        shares_or_contracts=units,
    )


def position_loss_pct(*, current_value_usd: float, cost_basis_usd: float) -> float:
    """Positive number = loss percentage of position NAV. Negative = gain."""
    if cost_basis_usd <= 0:
        return 0.0
    return (cost_basis_usd - current_value_usd) / cost_basis_usd * 100.0


def should_kill_position(
    *,
    current_value_usd: float,
    cost_basis_usd: float,
    is_option: bool,
    extra_kill: dict | None = None,
    spot_price: float | None = None,
) -> tuple[bool, str]:
    """Evaluate kill conditions for a single position. Returns (kill?, reason)."""
    cap = MAX_OPTION_LOSS_PCT if is_option else MAX_POSITION_LOSS_PCT
    loss = position_loss_pct(
        current_value_usd=current_value_usd, cost_basis_usd=cost_basis_usd
    )
    if loss >= cap:
        return True, f"loss {loss:.1f}% ≥ {cap:.0f}% cap"
    if extra_kill and spot_price is not None:
        below = extra_kill.get("underlying_price_below")
        if below is not None and spot_price <= below:
            return True, f"spot {spot_price} ≤ kill_below {below}"
        above = extra_kill.get("underlying_price_above")
        if above is not None and spot_price >= above:
            return True, f"spot {spot_price} ≥ kill_above {above}"
    return False, ""


def daily_circuit_breaker_tripped(
    *, sod_nav_usd: float, current_nav_usd: float
) -> tuple[bool, float]:
    """≥8% drop on the day halts new orders. Returns (tripped?, dd_pct)."""
    if sod_nav_usd <= 0:
        return False, 0.0
    dd = (sod_nav_usd - current_nav_usd) / sod_nav_usd * 100.0
    return dd >= DAILY_DD_HALT_PCT, dd


def position_band_ok(count: int, all_cash: bool) -> bool:
    if all_cash:
        return count == 0
    lo, hi = TARGET_POSITION_BAND
    return lo <= count <= hi


def total_position_pct(positions: list[dict]) -> float:
    return sum(p.get("position_pct", 0.0) for p in positions)
