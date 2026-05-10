"""Options helpers — Black-Scholes Greeks, IV solve, IV percentile, liquidity.

Stdlib-only Black-Scholes (uses math.erf) so the schema/risk/options test
suite has no heavy dependencies. Chain fetching delegates to alpaca-py via
lib/alpaca_client; HV inputs come from lib/market_data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

OptionType = Literal["call", "put"]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1(s: float, k: float, t: float, r: float, q: float, sigma: float) -> float:
    return (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


@dataclass(frozen=True)
class BSInputs:
    spot: float
    strike: float
    t_years: float       # time to expiry in years
    rate: float = 0.045  # risk-free
    div_yield: float = 0.0
    sigma: float = 0.20  # IV (annualised)
    kind: OptionType = "call"


def bs_price(inp: BSInputs) -> float:
    s, k, t, r, q, sig = inp.spot, inp.strike, inp.t_years, inp.rate, inp.div_yield, inp.sigma
    if t <= 0 or sig <= 0:
        intrinsic = max(0.0, (s - k) if inp.kind == "call" else (k - s))
        return intrinsic
    d1 = _d1(s, k, t, r, q, sig)
    d2 = d1 - sig * math.sqrt(t)
    if inp.kind == "call":
        return s * math.exp(-q * t) * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * math.exp(-q * t) * _norm_cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float


def greeks(inp: BSInputs) -> Greeks:
    s, k, t, r, q, sig = inp.spot, inp.strike, inp.t_years, inp.rate, inp.div_yield, inp.sigma
    if t <= 0 or sig <= 0:
        return Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)
    d1 = _d1(s, k, t, r, q, sig)
    d2 = d1 - sig * math.sqrt(t)
    pdf = _norm_pdf(d1)
    if inp.kind == "call":
        delta = math.exp(-q * t) * _norm_cdf(d1)
        theta = (
            -(s * pdf * sig * math.exp(-q * t)) / (2 * math.sqrt(t))
            - r * k * math.exp(-r * t) * _norm_cdf(d2)
            + q * s * math.exp(-q * t) * _norm_cdf(d1)
        )
    else:
        delta = math.exp(-q * t) * (_norm_cdf(d1) - 1.0)
        theta = (
            -(s * pdf * sig * math.exp(-q * t)) / (2 * math.sqrt(t))
            + r * k * math.exp(-r * t) * _norm_cdf(-d2)
            - q * s * math.exp(-q * t) * _norm_cdf(-d1)
        )
    gamma = math.exp(-q * t) * pdf / (s * sig * math.sqrt(t))
    vega = s * math.exp(-q * t) * pdf * math.sqrt(t)
    # Convert theta to per-day, vega to per-1pt-IV — convention matches the
    # bracket reported by chain providers and used in the schemas.
    return Greeks(delta=delta, gamma=gamma, theta=theta / 365.0, vega=vega / 100.0)


def implied_vol(
    *,
    market_price: float,
    spot: float,
    strike: float,
    t_years: float,
    rate: float = 0.045,
    div_yield: float = 0.0,
    kind: OptionType = "call",
    tol: float = 1e-4,
    max_iter: int = 80,
) -> float:
    """Bisection IV solver. Returns 0.0 if the price is below intrinsic."""
    intrinsic = max(0.0, (spot - strike) if kind == "call" else (strike - spot))
    if market_price < intrinsic - 1e-6 or t_years <= 0:
        return 0.0
    lo, hi = 1e-4, 5.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        px = bs_price(BSInputs(spot, strike, t_years, rate, div_yield, mid, kind))
        if abs(px - market_price) < tol:
            return mid
        if px > market_price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def iv_percentile(current_iv: float, history_iv: list[float]) -> float:
    """Percent of historical observations below current_iv. Returns 0–100."""
    if not history_iv:
        return 0.0
    below = sum(1 for v in history_iv if v < current_iv)
    return 100.0 * below / len(history_iv)


def passes_chain_liquidity(
    *,
    bid: float,
    ask: float,
    open_interest: int,
    min_oi: int = 100,
    max_spread_pct: float = 15.0,
) -> bool:
    """Reject illiquid contracts. Caller should also enforce per-symbol ADV."""
    if bid <= 0 or ask <= 0 or open_interest < min_oi:
        return False
    mid = 0.5 * (bid + ask)
    spread_pct = (ask - bid) / mid * 100.0
    return spread_pct <= max_spread_pct
