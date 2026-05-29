"""Live-mark helpers — turn broker positions into the `marks` dict the
P&L and monitor code expects.

Mark key convention (kept consistent across dashboard, monitor, and PnL):
  - ETF positions are keyed by ETF symbol, e.g. "TQQQ".

The system is ETF-only (options were removed). `current_value_usd` is the
broker-reported market value for the leg as a whole (shares × last price).
We prefer the broker's `current_price` field directly when available — it's
the per-share live quote with no rounding hazard.
"""
from __future__ import annotations

from .broker import Broker, BrokerPosition


def _key_for_broker_position(p: BrokerPosition) -> str:
    """Translate a broker position into the key shape consumers look up by.

    ETF positions key on the bare symbol. A stray legacy option position
    (should not occur — the system is ETF-only) keys on its raw symbol so
    nothing crashes; monitor flattens it as an unsupported instrument.
    """
    return p.symbol


def marks_from_positions(positions: list[BrokerPosition]) -> dict[str, float]:
    """Pure helper: turn a list of broker positions into a marks dict
    keyed by ETF symbol. Use this when the caller has already fetched
    positions (to share a single get_positions round-trip between marks +
    cost basis) and wants exceptions to propagate.
    """
    out: dict[str, float] = {}
    for p in positions:
        if p.qty == 0:
            continue
        # Prefer current_price when Alpaca reports it — that's the live
        # per-share quote, direct from the position object, no rounding
        # hazard. Older alpaca-py SDKs and test stubs may not populate it,
        # so keep the market_value/qty fallback for those paths.
        if p.current_price is not None:
            per_unit = p.current_price
        else:
            try:
                per_unit = p.market_value / p.qty
            except ZeroDivisionError:
                continue
        out[_key_for_broker_position(p)] = per_unit
    return out


def cost_basis_from_positions(positions: list[BrokerPosition]) -> dict[str, float]:
    """Pure helper: turn broker positions into a cost-basis dict.

    Same key convention + same qty/cost filters as cost_basis_from_broker.
    Use this when the caller wants exceptions to propagate (e.g. so the
    dashboard can flag broker-unreachable rather than silently treating
    every position as closed).
    """
    out: dict[str, float] = {}
    for p in positions:
        if p.qty == 0:
            continue
        if p.avg_cost is None:
            continue
        out[_key_for_broker_position(p)] = p.avg_cost
    return out


def marks_from_broker(broker: Broker) -> dict[str, float]:
    """Build a {symbol: per_share_price} dict from broker.get_positions().

    Safe to call when broker is None or get_positions fails — returns {} in
    both cases so callers don't have to special-case missing-broker paths.
    Callers that NEED to distinguish "broker says zero positions" from
    "broker call failed" should use marks_from_positions on a positions
    list they fetched themselves, so exceptions propagate.
    """
    if broker is None:
        return {}
    try:
        positions = broker.get_positions()
    except Exception:
        return {}
    return marks_from_positions(positions)


def cost_basis_from_broker(broker: Broker) -> dict[str, float]:
    """Build a {symbol: per_share_cost_basis} dict from broker.get_positions().

    Per-share cost basis comes from Alpaca's `avg_cost` — the actual fill
    price the broker recorded. Dashboards / P&L code prefer this when
    computing realised vs intended-vs-actual P&L.

    Safe to call when broker is None or get_positions fails — returns {}
    in both cases.
    """
    if broker is None:
        return {}
    try:
        positions = broker.get_positions()
    except Exception:
        return {}
    return cost_basis_from_positions(positions)


def portfolio_to_mark_keys(portfolio: dict) -> dict[str, str]:
    """Map each portfolio position to the mark key (ETF symbol) it should
    look up. Mirrors the keying used in pnl.compute_portfolio_pnl. Returns
    {position_index_str: symbol} so the dashboard can show "marked /
    not-marked" per position.
    """
    out: dict[str, str] = {}
    for i, pos in enumerate(portfolio.get("positions", [])):
        out[str(i)] = pos["symbol"]
    return out
