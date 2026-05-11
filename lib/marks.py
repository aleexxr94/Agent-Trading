"""Live-mark helpers — turn broker positions into the `marks` dict the
P&L and monitor code expects.

Mark key convention (kept consistent across dashboard, monitor, and PnL):
  - ETF positions:   keyed by ETF symbol, e.g. "TQQQ"
  - Option positions: keyed by f"{underlying}|{strike}|{expiry}|{type}"

`current_value_usd` is the broker-reported market value for the leg as a whole
(shares × last price for ETFs; contracts × premium × 100 for options). To
build a marks dict suitable for pnl.compute_portfolio_pnl, we divide by the
unit count to get per-share / per-contract.
"""
from __future__ import annotations

from .broker import Broker, BrokerPosition


def _key_for_broker_position(p: BrokerPosition) -> str:
    """Alpaca returns options symbols like 'SPY261219C00530000' — OCC OSI.
    For now we key options by the OSI symbol directly; the orchestrator
    populates the same OSI in portfolio.json's positions when orders land
    (Phase 10c). Until then options marks aren't matched — a documented gap."""
    return p.symbol


def marks_from_broker(broker: Broker) -> dict[str, float]:
    """Build a {key: per_unit_price} dict from broker.get_positions().

    Safe to call when broker is None or get_positions fails — returns {} in
    both cases so callers don't have to special-case missing-broker paths.
    """
    if broker is None:
        return {}
    try:
        positions = broker.get_positions()
    except Exception:
        return {}

    out: dict[str, float] = {}
    for p in positions:
        if p.qty == 0:
            continue
        # market_value / qty gives the per-share (ETF) or per-contract-dollar
        # (option, where Alpaca's market_value already absorbs the 100x).
        try:
            per_unit = p.market_value / p.qty
        except ZeroDivisionError:
            continue
        # For options: pnl.compute_position_pnl expects per-share premium
        # (matches the schema's premium_paid units), so divide by the 100x.
        if p.asset_class == "us_option":
            per_unit = per_unit / 100.0
        out[_key_for_broker_position(p)] = per_unit
    return out


def portfolio_to_mark_keys(portfolio: dict) -> dict[str, str]:
    """Map each portfolio position to the mark key it should look up.

    Mirrors the keying used in pnl.compute_portfolio_pnl. Returns
    {position_index_str: key} so the dashboard can show "marked / not-marked"
    per position.
    """
    out: dict[str, str] = {}
    for i, pos in enumerate(portfolio.get("positions", [])):
        if pos["kind"] == "etf":
            out[str(i)] = pos["symbol"]
        else:
            out[str(i)] = f"{pos['underlying']}|{pos['strike']}|{pos['expiry']}|{pos['type']}"
    return out
