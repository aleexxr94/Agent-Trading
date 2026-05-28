"""Risk primitives — sizing, kill conditions, circuit breakers.

Shared by orchestrator.py (sizing + cap checks at construction time) and
monitor.py (kill-condition evaluation between runs).

Spec invariants:
  - Per-position cap at entry: ≤15% of portfolio NAV.
  - Per-position kill: ≤25% loss of position NAV (or 100% premium for long options).
  - Daily portfolio drawdown circuit breaker: ≥8% in a single UTC day halts new orders.
  - Target band 1–12 positions (or all-cash) — judgement call by the agent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone


def dd_breaker_enabled() -> bool:
    """Kill-switch for the Phase 2 automatic 8% daily-drawdown breaker.
    Default ON. Set DD_BREAKER_ENABLED=false to disable both the monitor's
    halt-flag write and the orchestrator's order gating without redeploying."""
    return os.environ.get("DD_BREAKER_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def parse_iso_utc(s: str | None) -> datetime | None:
    """Tolerant ISO-8601 → aware-UTC parse. Returns None on anything
    unparseable. Shared by monitor.py (shadow + enforcement) so the
    time-stop format handling lives in one place."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# Hard, repo-wide constants — do not parameterise without a CLAUDE.md change.
MAX_POSITION_PCT = 15.0
MAX_POSITION_LOSS_PCT = 25.0
MAX_OPTION_LOSS_PCT = 100.0
DAILY_DD_HALT_PCT = 8.0
TARGET_POSITION_BAND = (1, 12)


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
    now_utc: datetime | None = None,
) -> tuple[bool, str]:
    """Evaluate kill conditions for a single position. Returns (kill?, reason).

    Checks, in order: the hard loss cap (25% ETF / 100% option), the
    underlying price stops (when ``spot_price`` is known), and the
    ``time_stop_utc`` time stop (when set). ``now_utc`` defaults to the
    current UTC time; callers may inject it for deterministic tests.
    """
    cap = MAX_OPTION_LOSS_PCT if is_option else MAX_POSITION_LOSS_PCT
    loss = position_loss_pct(
        current_value_usd=current_value_usd, cost_basis_usd=cost_basis_usd
    )
    if loss >= cap:
        return True, f"loss {loss:.1f}% ≥ {cap:.0f}% cap"
    if extra_kill:
        if spot_price is not None:
            below = extra_kill.get("underlying_price_below")
            if below is not None and spot_price <= below:
                return True, f"spot {spot_price} ≤ kill_below {below}"
            above = extra_kill.get("underlying_price_above")
            if above is not None and spot_price >= above:
                return True, f"spot {spot_price} ≥ kill_above {above}"
        ts = extra_kill.get("time_stop_utc")
        if ts:
            t = parse_iso_utc(ts)
            now = now_utc or datetime.now(timezone.utc)
            if t is not None and now >= t:
                return True, f"time stop {ts} reached"
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


# Drawdown-adaptive sizing — when the account has bled materially from
# its recent peak, halve the per-position cap so a string of losses
# doesn't compound. Linear interpolation between the two anchor points
# below; clipped at both ends.
ADAPTIVE_CAP_BASE_PCT = 15.0      # at-peak NAV → spec-mandated 15%
ADAPTIVE_CAP_FLOOR_PCT = 7.5      # ≥10% drawdown → halved
ADAPTIVE_DRAWDOWN_TRIGGER = 0.10  # 10% off peak = full reduction


def adaptive_position_cap_pct(*, current_nav: float, peak_nav_30d: float) -> float:
    """Per-position % cap adjusted for current drawdown.

    Returns 15.0 when current NAV ≥ peak NAV.
    Returns 7.5 when current NAV is ≥10% below peak.
    Linear between those endpoints.

    Both args expect raw USD NAV. If peak is unknown / non-positive
    (cold-start path), returns the base cap.
    """
    if peak_nav_30d <= 0 or current_nav >= peak_nav_30d:
        return ADAPTIVE_CAP_BASE_PCT
    drawdown_frac = max(0.0, (peak_nav_30d - current_nav) / peak_nav_30d)
    if drawdown_frac >= ADAPTIVE_DRAWDOWN_TRIGGER:
        return ADAPTIVE_CAP_FLOOR_PCT
    # Linear interpolation between (0%, BASE) and (TRIGGER, FLOOR).
    fraction = drawdown_frac / ADAPTIVE_DRAWDOWN_TRIGGER
    return ADAPTIVE_CAP_BASE_PCT - fraction * (ADAPTIVE_CAP_BASE_PCT - ADAPTIVE_CAP_FLOOR_PCT)
