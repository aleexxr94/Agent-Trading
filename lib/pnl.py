"""Modelled trading costs + gross/net P&L.

Alpaca paper charges nothing, but the spec is to **paper-trade with costs
that mirror live**, so paper Sharpe is honest and the promote-to-live
decision (CLAUDE.md §Promotion to live) isn't built on inflated numbers.

The cost numbers come from ``lib.alpaca_costs`` — the single source of truth,
calibrated to a live USD-funded **Alpaca** account (commission-free; SEC + FINRA
TAF sell-side only; configurable per-side slippage). This module wraps that model
into per-position breakdowns for the dashboard and the synthetic balance.

Per leg the cost is: slippage (both sides) + commission ($0 on Alpaca); the
sell leg additionally pays the regulatory fees. Helpers expose the entry leg
(slippage only) and the exit leg (slippage + regulatory) so callers can avoid
double-counting once a fill's entry-leg cost is already recorded in trades.jsonl.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import alpaca_costs

@dataclass(frozen=True)
class TradeCost:
    """Per-position trading-cost breakdown sourced from ``lib.alpaca_costs``.

    - ``half_spread_usd``: per-leg slippage (×2 for a full round-trip).
    - ``commission_usd``:  per-leg commission ($0 on Alpaca; kept for shape).
    - ``reg_fees_usd``:    sell-side regulatory fees (applied once, on exit).

    Leg helpers let callers add only the part that hasn't been paid yet:
    ``entry_leg_usd`` (slippage + commission) is incurred on the buy;
    ``exit_leg_usd`` (slippage + commission + reg fees) on the sell.
    """
    notional_usd: float
    half_spread_usd: float           # per leg slippage (×2 for round-trip)
    commission_usd: float            # per leg (×2 for round-trip); $0 on Alpaca
    reg_fees_usd: float = 0.0        # sell-side only (incurred once, on exit)

    @property
    def entry_leg_usd(self) -> float:
        return self.half_spread_usd + self.commission_usd

    @property
    def exit_leg_usd(self) -> float:
        return self.half_spread_usd + self.commission_usd + self.reg_fees_usd

    @property
    def round_trip_usd(self) -> float:
        return self.entry_leg_usd + self.exit_leg_usd


def model_etf_cost(
    *, shares: float, price_usd: float, symbol: str | None = None
) -> TradeCost:
    """Per-leg cost for one ETF position, from the Alpaca cost model. Use
    `.round_trip_usd` for a full open+close (sell-side reg fees counted once),
    or `.exit_leg_usd` for just the projected close. ``symbol`` selects the
    per-ticker slippage assumption (thin names cost more)."""
    notional = shares * price_usd
    half_spread = alpaca_costs.slippage_cost(symbol=symbol, notional=notional)
    commission = alpaca_costs.COMMISSION_PER_TRADE
    reg_fees = alpaca_costs.regulatory_sell_fee(
        sell_notional=notional, shares_sold=shares,
    )
    return TradeCost(
        notional_usd=notional,
        half_spread_usd=half_spread,
        commission_usd=commission,
        reg_fees_usd=reg_fees,
    )


def model_position_cost(position: dict) -> TradeCost:
    """Per-leg cost for one ETF position from the position.schema.json shape.
    Multiply by 2 (or use `.round_trip_usd`) for a full open+close round-trip
    — the round_trip_usd property already accounts for sell-side reg fees."""
    return model_etf_cost(
        shares=position["shares"],
        price_usd=position["avg_cost"],
        symbol=position.get("symbol"),
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
    actual_cost_per_unit: float | None = None,
) -> PnLBreakdown:
    """Unrealised P&L for an open ETF position. Returns gross, modelled cost, and net.

    current_mark_usd:
      - Per-share price (USD).
      - Pass None when no mark is available — gross is 0 and modelled cost is
        still computed (entry leg already paid).

    actual_cost_per_unit (optional override):
      - When provided, overrides the cost basis pulled from the position dict
        (per-share fill price, replacing position["avg_cost"]). Use Alpaca's
        reported `avg_cost` here to compute P&L against the real fill. Pass
        None to keep the historical behaviour.
    """
    cost = model_position_cost(position)
    if current_mark_usd is None:
        # Entry leg already paid: half-spread + commission (no sell-side fees yet).
        entry_leg_cost = cost.half_spread_usd + cost.commission_usd
        return PnLBreakdown(gross_pnl_usd=0.0, modelled_costs_usd=entry_leg_cost)
    basis = actual_cost_per_unit if actual_cost_per_unit is not None else position["avg_cost"]
    gross = (current_mark_usd - basis) * position["shares"]
    return PnLBreakdown(gross_pnl_usd=gross, modelled_costs_usd=cost.round_trip_usd)


def compute_portfolio_pnl(
    *,
    portfolio: dict,
    marks: dict[str, float] | None = None,
    costs: dict[str, float] | None = None,
) -> PnLBreakdown:
    """Aggregate gross / modelled-cost / net across all open ETF positions.

    `marks` keys: ETF symbol. `costs` (optional): same keying as marks,
    holds the actual per-share fill prices from the broker. When provided,
    P&L uses the broker's cost basis rather than the agent's `avg_cost`
    estimate.
    """
    marks = marks or {}
    costs = costs or {}
    gross = 0.0
    cost_total = 0.0
    for p in portfolio.get("positions", []):
        key = p["symbol"]
        b = compute_position_pnl(
            position=p,
            current_mark_usd=marks.get(key),
            actual_cost_per_unit=costs.get(key),
        )
        gross += b.gross_pnl_usd
        cost_total += b.modelled_costs_usd
    return PnLBreakdown(gross_pnl_usd=gross, modelled_costs_usd=cost_total)
