"""Alpaca live-cost model for friction-honest paper trading.

Alpaca **paper** reports $0 fees and models no slippage, so a raw paper run
overstates returns versus a live USD-funded Alpaca account. This module is the
canonical cost model the rest of the system uses to net those frictions out, so
paper Sharpe reflects what a live Alpaca account would actually keep (CLAUDE.md
§Promotion to live).

Scope for THIS strategy (long-only leveraged/inverse ETFs, cash account):
  - Commission:        $0 — Alpaca retail US-listed ETFs are commission-free.
  - Regulatory fees:   SEC §31 + FINRA TAF, **sell-side only**, <1 bp.
  - Slippage/spread:   the dominant real cost; modelled per side, with
                       per-ticker overrides for thin names.
  - Margin interest / borrow: $0 and N/A (cash account, no shorts — bearish
                       views are long inverse ETFs). Stubs kept for optionality.

Rates change periodically (SEC resets when its appropriation is hit; FINRA
updates each January; CAT is currently suspended) so every rate is env-overridable
— NOT hardcoded as the single source of truth.
"""
from __future__ import annotations

import os
from decimal import ROUND_UP, Decimal


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---- Commission (Alpaca retail US ETFs) ----
COMMISSION_PER_TRADE = _env_float("ALPACA_COMMISSION_PER_TRADE", 0.00)

# ---- Regulatory pass-through fees (SELLS only) ----
# SEC Section 31: $20.60 per $1,000,000 of sell principal (effective 2026-04-04;
# resets to $0 once the SEC hits its annual appropriation — keep configurable).
SEC_FEE_PER_DOLLAR = _env_float("ALPACA_SEC_FEE_PER_DOLLAR", 0.0000206)
# FINRA TAF: per-share on sells, capped per trade (FINRA rate effective 2026-01-01).
TAF_PER_SHARE = _env_float("ALPACA_TAF_PER_SHARE", 0.000195)
TAF_CAP_PER_TRADE = _env_float("ALPACA_TAF_CAP_PER_TRADE", 9.79)
# CAT (Consolidated Audit Trail): suspended since 2025-12-01. Flag kept so it can
# be switched back on (per share, buys + sells) without a code change.
CAT_PER_SHARE = _env_float("ALPACA_CAT_PER_SHARE", 0.00)

# ---- Slippage / bid-ask spread (per side, in bps of notional) ----
# Liquid names are ~penny-wide; thin names cost far more per round trip. Default
# applies unless the symbol has an override below. Override the default globally
# via SLIPPAGE_BPS_PER_SIDE.
SLIPPAGE_BPS_PER_SIDE = _env_float("SLIPPAGE_BPS_PER_SIDE", 2.0)
# Per-ticker slippage (bps/side) for the thin lines flagged in the cost reference.
SLIPPAGE_OVERRIDES: dict[str, float] = {
    "HIBL": 15.0, "HIBS": 15.0,
    "WEBL": 15.0, "WEBS": 15.0,
    "KOLD": 15.0, "BOIL": 12.0,
    "DRIP": 15.0, "GUSH": 12.0,
    "GLL": 15.0, "UGL": 12.0,
    "ZSL": 15.0, "AGQ": 12.0,
    "DPST": 15.0, "NAIL": 12.0,
    "CURE": 12.0, "DFEN": 12.0,
    "NVD": 12.0, "TSLZ": 12.0, "MSTZ": 12.0,
    "ETHD": 12.0, "BITI": 12.0,
}

BPS = 10_000


def ceil_to_cent(x: float) -> float:
    """Round UP to the nearest cent, the way regulatory fees are assessed."""
    if x <= 0:
        return 0.0
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_UP))


def slippage_bps_for(symbol: str | None) -> float:
    """Per-side slippage assumption (bps) for a symbol, falling back to the
    universe default. Symbol is matched case-insensitively."""
    if symbol:
        return SLIPPAGE_OVERRIDES.get(symbol.upper(), SLIPPAGE_BPS_PER_SIDE)
    return SLIPPAGE_BPS_PER_SIDE


def slippage_cost(*, symbol: str | None, notional: float) -> float:
    """Per-side slippage/spread cost in USD for one leg of `notional`."""
    return abs(notional) * (slippage_bps_for(symbol) / BPS)


def regulatory_sell_fee(*, sell_notional: float, shares_sold: float) -> float:
    """SEC + FINRA TAF + CAT for one SELL order. Buys are $0.

        sec_fee  = ceil_cent(sell_notional * SEC_FEE_PER_DOLLAR)
        taf_fee  = min(ceil_cent(shares_sold * TAF_PER_SHARE), TAF_CAP_PER_TRADE)
        cat_fee  = shares_sold * CAT_PER_SHARE
    """
    sec_fee = ceil_to_cent(abs(sell_notional) * SEC_FEE_PER_DOLLAR)
    taf_fee = min(ceil_to_cent(abs(shares_sold) * TAF_PER_SHARE), TAF_CAP_PER_TRADE)
    cat_fee = abs(shares_sold) * CAT_PER_SHARE
    return sec_fee + taf_fee + cat_fee


def fill_cost(
    *, side: str, symbol: str | None, shares: float, price: float
) -> tuple[float, float]:
    """Modelled cost for ONE fill leg. Returns ``(fee_usd, slippage_usd)``.

    - ``fee_usd``: commission ($0) + regulatory (sell-side only).
    - ``slippage_usd``: per-side spread cost (both buys and sells; Alpaca never
      reports this, even live).
    """
    notional = abs(shares) * price
    slip = slippage_cost(symbol=symbol, notional=notional)
    fee = COMMISSION_PER_TRADE
    if str(side).lower() == "sell":
        fee += regulatory_sell_fee(sell_notional=notional, shares_sold=shares)
    return fee, slip


# ---- Stubs: $0 and not applicable to a cash, long-only account ----
# Bearish exposure is taken by buying inverse ETFs, never by shorting, and the
# agent never trades on margin (it sizes against a synthetic NAV, not buying
# power). These hooks exist so a future short-enabling change has an obvious
# seam — they MUST stay $0 while the system is cash + long-only.
def margin_interest_cost(*, daily_debit_usd: float, apr: float = 0.065) -> float:
    """$0 — the account carries no debit balance (cash, no margin)."""
    return 0.0


def borrow_fee(*, symbol: str, notional: float, days: float) -> float:
    """$0 — the account never shorts (bearish = long inverse ETF)."""
    return 0.0
