"""Modelled trading costs + gross/net P&L.

Alpaca paper charges nothing, but the spec is to **paper-trade with costs
that mirror live**, so paper Sharpe is honest and the promote-to-live
decision (CLAUDE.md §Promotion to live) isn't built on inflated numbers.

Calibration target: **IBKR Pro tier, UK retail, USD-funded account** —
that's the most realistic live broker once promote-to-live triggers.

Components modelled per round-trip:

  ETFs (per leg):
    + half-spread:    5 bps of notional (each side, ~10 bps RT)
    + commission:     max(\$1, qty × \$0.005), capped at 0.5% of notional
                      (IBKR Pro: \$0.005/share, \$1 min, 0.5% max)
  ETFs (sell-side only, added once):
    + SEC fee:        $0.0000278 × sale notional
    + FINRA TAF:      \$0.000166/share, capped at \$9.90/trade

  Options (per leg):
    + half-spread:    25 bps of premium (each side, ~50 bps RT)
    + commission:     \$0.65/contract (IBKR Pro)
    + OCC fee:        \$0.04/contract
  Options (sell-side only, added once):
    + SEC fee:        $0.0000278 × premium notional sold

Why this matters on a £2k account: the \$1 IBKR minimum commission dominates
small positions. On a $250 trade, commission alone is **40 bps** — 4× the
modelled half-spread. Ignoring it makes paper Sharpe ~0.5 too generous.

All constants are module-level so they're easy to override for a different
broker profile (Lite, retail US broker, etc.) without env-var plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- ETF cost model (IBKR Pro retail USD) ----
ETF_HALF_SPREAD_BPS = 5.0            # 5 bps each side on liquid leveraged ETFs
ETF_PER_SHARE_COMMISSION = 0.005     # IBKR Pro: $0.005/share
ETF_MIN_COMMISSION_USD = 1.00        # IBKR Pro: $1 minimum per trade
ETF_MAX_COMMISSION_PCT = 0.5         # IBKR Pro: max 0.5% of trade value

# ---- Options cost model (IBKR Pro retail) ----
OPTION_HALF_SPREAD_BPS = 25.0        # options chains are wider than equities
OPTION_PER_CONTRACT_USD = 0.65       # IBKR Pro: $0.65/contract
OPTION_OCC_FEE_PER_CONTRACT = 0.04   # OCC exchange clearing fee

# ---- Regulatory fees (sell-side only) ----
SEC_FEE_PER_USD_SOLD = 0.0000278     # SEC §31 fee on sales (equities + options)
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


def _ibkr_etf_commission_per_leg(*, shares: float, notional: float) -> float:
    """IBKR Pro tier: per_share × shares, floored at $1, capped at 0.5%."""
    raw = shares * ETF_PER_SHARE_COMMISSION
    capped = min(raw, notional * (ETF_MAX_COMMISSION_PCT / 100.0))
    return max(ETF_MIN_COMMISSION_USD, capped)


def model_etf_cost(*, shares: float, price_usd: float) -> TradeCost:
    """Per-leg cost for one ETF position. Use `.round_trip_usd` for full
    open+close; the round-trip includes sell-side regulatory fees once."""
    notional = shares * price_usd
    half_spread = notional * (ETF_HALF_SPREAD_BPS / BPS)
    commission = _ibkr_etf_commission_per_leg(shares=shares, notional=notional)
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


def model_option_cost(*, contracts: int, premium_usd: float) -> TradeCost:
    """Per-leg cost for one option position. premium_usd is per-contract
    dollar value (already inclusive of the 100x multiplier)."""
    notional = contracts * premium_usd
    half_spread = notional * (OPTION_HALF_SPREAD_BPS / BPS)
    # IBKR Pro options: $0.65/contract + OCC clearing $0.04/contract
    commission = contracts * (OPTION_PER_CONTRACT_USD + OPTION_OCC_FEE_PER_CONTRACT)
    sec_fee = notional * SEC_FEE_PER_USD_SOLD  # SEC §31 also applies to options sales
    return TradeCost(
        notional_usd=notional,
        half_spread_usd=half_spread,
        commission_usd=commission,
        reg_fees_usd=sec_fee,
    )


def model_position_cost(position: dict) -> TradeCost:
    """Per-leg cost for one position from the position.schema.json shape.
    Multiply by 2 (or use `.round_trip_usd`) for a full open+close round-trip
    — the round_trip_usd property already accounts for sell-side reg fees."""
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
        # Entry leg already paid: half-spread + commission (no sell-side fees yet).
        entry_leg_cost = cost.half_spread_usd + cost.commission_usd
        return PnLBreakdown(gross_pnl_usd=0.0, modelled_costs_usd=entry_leg_cost)
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
