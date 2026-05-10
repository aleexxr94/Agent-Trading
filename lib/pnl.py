"""Modelled trading costs + gross/net P&L.

Alpaca paper charges nothing, but CLAUDE.md §Promotion to live requires
"Sharpe ≥ 0.5 on paper after modelled costs" before any live promotion.
This module is where that modelled cost lives — calibrated to roughly what
a UK retail broker (IBKR) would charge once the broker swap-point in
lib/broker.py is wired up.

Defaults are deliberately conservative — better to over-estimate friction
than under-estimate it on a £2k account where 1 bp matters.
"""
from __future__ import annotations

from dataclasses import dataclass

# Round-trip half-spread on liquid leveraged ETFs (~5 bps each side, 10 bps RT).
# Configurable via CLAUDE.md if real fills come in tighter.
ETF_HALF_SPREAD_BPS = 5.0
ETF_COMMISSION_BPS = 0.0  # Alpaca paper / IBKR Pro tier are effectively free.

# Per-contract option fee (IBKR fixed: $0.70/contract; rounded down for SPY/QQQ
# liquidity). Both legs of an open+close round-trip count.
OPTION_PER_CONTRACT_USD = 0.65
OPTION_HALF_SPREAD_BPS = 25.0  # options chains are wider than equities

BPS = 10_000


@dataclass(frozen=True)
class TradeCost:
    notional_usd: float
    half_spread_usd: float
    commission_usd: float

    @property
    def round_trip_usd(self) -> float:
        return 2.0 * (self.half_spread_usd + self.commission_usd)


def model_etf_cost(*, shares: float, price_usd: float) -> TradeCost:
    """One leg of an ETF trade. Round-trip is 2× the returned cost."""
    notional = shares * price_usd
    half_spread = notional * (ETF_HALF_SPREAD_BPS / BPS)
    commission = notional * (ETF_COMMISSION_BPS / BPS)
    return TradeCost(notional_usd=notional, half_spread_usd=half_spread, commission_usd=commission)


def model_option_cost(*, contracts: int, premium_usd: float) -> TradeCost:
    """One leg of an option trade. Premium is per-contract dollar value (already
    inclusive of the 100x multiplier). Round-trip is 2× the returned cost."""
    notional = contracts * premium_usd
    half_spread = notional * (OPTION_HALF_SPREAD_BPS / BPS)
    commission = contracts * OPTION_PER_CONTRACT_USD
    return TradeCost(notional_usd=notional, half_spread_usd=half_spread, commission_usd=commission)


def model_position_cost(position: dict) -> TradeCost:
    """Per-leg cost for one position from the position.schema.json shape.
    Multiply by 2 (or use `.round_trip_usd`) for a full open+close round-trip."""
    if position["kind"] == "etf":
        return model_etf_cost(shares=position["shares"], price_usd=position["avg_cost"])
    return model_option_cost(
        contracts=position["contracts"],
        premium_usd=position["premium_paid"] * 100,
    )


@dataclass(frozen=True)
class PnLBreakdown:
    gross_pnl_usd: float
    modelled_costs_usd: float

    @property
    def net_pnl_usd(self) -> float:
        return self.gross_pnl_usd - self.modelled_costs_usd


def compute_position_pnl(
    *,
    position: dict,
    current_mark_usd: float | None,
) -> PnLBreakdown:
    """Unrealised P&L for an open position. Returns gross, modelled cost, and net.

    current_mark_usd:
      - For ETFs: per-share price (USD).
      - For options: per-contract premium (USD), the same units as `premium_paid`.
      - Pass None when no mark is available — gross is 0 and modelled cost is
        still computed (entry leg already paid).
    """
    cost = model_position_cost(position)
    if current_mark_usd is None:
        return PnLBreakdown(gross_pnl_usd=0.0, modelled_costs_usd=cost.round_trip_usd / 2)
    if position["kind"] == "etf":
        gross = (current_mark_usd - position["avg_cost"]) * position["shares"]
    else:
        gross = (current_mark_usd - position["premium_paid"]) * position["contracts"] * 100
    return PnLBreakdown(gross_pnl_usd=gross, modelled_costs_usd=cost.round_trip_usd)


def compute_portfolio_pnl(
    *,
    portfolio: dict,
    marks: dict[str, float] | None = None,
) -> PnLBreakdown:
    """Aggregate gross / modelled-cost / net across all open positions.

    `marks` keys: ETF symbol for ETFs; for options use
    f"{underlying}|{strike}|{expiry}|{type}" — same convention as monitor.py.
    """
    marks = marks or {}
    gross = 0.0
    cost_total = 0.0
    for p in portfolio.get("positions", []):
        if p["kind"] == "etf":
            mark = marks.get(p["symbol"])
        else:
            key = f"{p['underlying']}|{p['strike']}|{p['expiry']}|{p['type']}"
            mark = marks.get(key)
        b = compute_position_pnl(position=p, current_mark_usd=mark)
        gross += b.gross_pnl_usd
        cost_total += b.modelled_costs_usd
    return PnLBreakdown(gross_pnl_usd=gross, modelled_costs_usd=cost_total)
