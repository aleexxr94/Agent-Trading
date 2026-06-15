"""Modelled trading costs + gross/net P&L.

Alpaca paper charges nothing, but the spec is to **paper-trade with costs
that mirror live**, so paper Sharpe is honest and the promote-to-live
decision (CLAUDE.md §Promotion to live) isn't built on inflated numbers.

This is a **conservative retail-friction model**. The live broker is Alpaca
(Alpaca live is available to UK retail for ETFs — see CLAUDE.md §Critical
preconditions #2), which is **commission-free**, so the commission leg below
intentionally **over-estimates** Alpaca's real cost and is kept as a
conservative floor. Making the estimate match Alpaca live exactly (zero
commission, current SEC/TAF rates, configurable slippage) is the separate
cost-accuracy task; the constants here are unchanged for now.

Components modelled per round-trip:

  ETFs (per leg):
    + half-spread:    5 bps of notional (each side, ~10 bps RT)
    + commission:     max($1, qty × $0.005), capped at 0.5% of notional
                      (conservative retail floor; Alpaca live is $0)
  ETFs (sell-side only, added once):
    + SEC fee:        $0.0000278 × sale notional
    + FINRA TAF:      $0.000166/share, capped at $9.90/trade

Why a conservative floor matters on a $2.5k account: the $1 minimum-commission
term dominates small positions. On a $250 trade, that term alone is **40 bps**
— 4× the modelled half-spread. Keeping it makes paper Sharpe a cautious
lower bound rather than a generous one. (Alpaca live would actually be cheaper
on the commission leg.)

All constants are module-level so they're easy to override for a different
cost profile without env-var plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- ETF cost model (conservative retail USD floor; Alpaca live is $0 commission) ----
ETF_HALF_SPREAD_BPS = 5.0            # 5 bps each side on liquid leveraged ETFs
ETF_PER_SHARE_COMMISSION = 0.005     # conservative retail floor: $0.005/share
ETF_MIN_COMMISSION_USD = 1.00        # conservative retail floor: $1 minimum per trade
ETF_MAX_COMMISSION_PCT = 0.5         # conservative retail floor: max 0.5% of trade value

# ---- Regulatory fees (sell-side only) ----
SEC_FEE_PER_USD_SOLD = 0.0000278     # SEC §31 fee on sales (equities)
FINRA_TAF_PER_SHARE_SOLD = 0.000166  # FINRA Trading Activity Fee, equities
FINRA_TAF_MAX_USD = 9.90             # FINRA TAF per-trade cap

BPS = 10_000


@dataclass(frozen=True)
class TradeCost:
    """Per-position trading-cost breakdown. round_trip_usd sums everything
    needed for the full open-then-close cycle: 2× spread + 2× commission +
    sell-side regulatory fees (which only apply once)."""
    notional_usd: float
    half_spread_usd: float           # per leg (×2 for round-trip)
    commission_usd: float            # per leg (×2 for round-trip)
    reg_fees_usd: float = 0.0        # total for round-trip (sell-side only)

    @property
    def round_trip_usd(self) -> float:
        return 2.0 * (self.half_spread_usd + self.commission_usd) + self.reg_fees_usd


def _retail_etf_commission_per_leg(*, shares: float, notional: float) -> float:
    """Conservative retail floor: per_share × shares, floored at $1, capped at 0.5%."""
    raw = shares * ETF_PER_SHARE_COMMISSION
    capped = min(raw, notional * (ETF_MAX_COMMISSION_PCT / 100.0))
    return max(ETF_MIN_COMMISSION_USD, capped)


def model_etf_cost(*, shares: float, price_usd: float) -> TradeCost:
    """Per-leg cost for one ETF position. Use `.round_trip_usd` for full
    open+close; the round-trip includes sell-side regulatory fees once."""
    notional = shares * price_usd
    half_spread = notional * (ETF_HALF_SPREAD_BPS / BPS)
    commission = _retail_etf_commission_per_leg(shares=shares, notional=notional)
    # Reg fees only on the sell side; rough approximation assumes sell at
    # entry price (the trade-level fee is small compared to spread anyway).
    sec_fee = notional * SEC_FEE_PER_USD_SOLD
    taf_fee = min(shares * FINRA_TAF_PER_SHARE_SOLD, FINRA_TAF_MAX_USD)
    return TradeCost(
        notional_usd=notional,
        half_spread_usd=half_spread,
        commission_usd=commission,
        reg_fees_usd=sec_fee + taf_fee,
    )


def model_position_cost(position: dict) -> TradeCost:
    """Per-leg cost for one ETF position from the position.schema.json shape.
    Multiply by 2 (or use `.round_trip_usd`) for a full open+close round-trip
    — the round_trip_usd property already accounts for sell-side reg fees."""
    return model_etf_cost(shares=position["shares"], price_usd=position["avg_cost"])


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
