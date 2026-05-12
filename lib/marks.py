"""Live-mark helpers — turn broker positions into the `marks` dict the
P&L and monitor code expects.

Mark key convention (kept consistent across dashboard, monitor, and PnL):
  - ETF positions:    keyed by ETF symbol, e.g. "TQQQ"
  - Option positions: keyed by the synthetic
        f"{underlying}|{strike}|{expiry}|{type}"
    matching pnl.compute_portfolio_pnl and monitor.evaluate_portfolio.
    Alpaca returns options as OCC OSI symbols (e.g. SPY261219C00530000);
    we reverse-parse them into the synthetic form so consumers can look
    up by the same key shape used throughout the schema.

`current_value_usd` is the broker-reported market value for the leg as a whole
(shares × last price for ETFs; contracts × premium × 100 for options). We
prefer the broker's `current_price` field directly when available — it's
the per-unit live quote with no rounding hazard.
"""
from __future__ import annotations

from .broker import Broker, BrokerPosition


def _osi_to_synthetic(osi: str) -> str | None:
    """Reverse the OSI symbol back into monitor/PnL's synthetic key shape.

    OSI format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE*1000:08d}
    Example:    SPY261219C00530000 -> SPY|530.0|2026-06-19|call

    Returns None if the symbol doesn't look like a valid OSI; the caller
    falls back to using the raw OSI as the key (better to mis-match than
    to crash on a malformed symbol).
    """
    if len(osi) < 15:
        return None
    strike_part = osi[-8:]
    type_char = osi[-9]
    yymmdd = osi[-15:-9]
    underlying = osi[:-15]
    if not (strike_part.isdigit() and yymmdd.isdigit() and underlying):
        return None
    if type_char not in ("C", "P"):
        return None
    strike = int(strike_part) / 1000.0
    type_ = "call" if type_char == "C" else "put"
    expiry = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return f"{underlying}|{strike}|{expiry}|{type_}"


def _key_for_broker_position(p: BrokerPosition) -> str:
    """Translate a broker position into the key shape consumers look up by.

    ETF: bare symbol. Option: synthetic 'UNDERLYING|STRIKE|EXPIRY|TYPE'
    (matching pnl.compute_portfolio_pnl). Falls back to the raw OSI when
    parsing fails so we never crash on a malformed symbol.
    """
    if p.asset_class == "us_option":
        syn = _osi_to_synthetic(p.symbol)
        return syn if syn is not None else p.symbol
    return p.symbol


def marks_from_positions(positions: list[BrokerPosition]) -> dict[str, float]:
    """Pure helper: turn a list of broker positions into a marks dict.

    Same key convention as marks_from_broker (ETF symbol or synthetic
    UNDERLYING|STRIKE|EXPIRY|TYPE). Use this when the caller has already
    fetched positions (e.g. to share a single get_positions round-trip
    between marks + cost basis) and wants exceptions to propagate.
    """
    out: dict[str, float] = {}
    for p in positions:
        if p.qty == 0:
            continue
        # Prefer current_price when Alpaca reports it — that's the live
        # per-share (ETF) or per-share-premium (option) quote, direct from
        # the position object, no rounding hazard. Older alpaca-py SDKs and
        # test stubs may not populate it, so keep the market_value/qty
        # fallback for those paths.
        if p.current_price is not None:
            per_unit = p.current_price
        else:
            try:
                per_unit = p.market_value / p.qty
            except ZeroDivisionError:
                continue
            # market_value bakes in the 100x option multiplier; strip it so
            # downstream pnl.compute_position_pnl sees per-share premium
            # (same units as schema's premium_paid).
            if p.asset_class == "us_option":
                per_unit = per_unit / 100.0
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
    """Build a {key: per_unit_price} dict from broker.get_positions().

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
    """Build a {key: per_unit_cost_basis} dict from broker.get_positions().

    Same key convention as marks_from_broker (ETF: symbol; option:
    synthetic UNDERLYING|STRIKE|EXPIRY|TYPE). Per-share / per-share-premium
    cost basis comes from Alpaca's `avg_cost` — the actual fill price the
    broker recorded, NOT the agent's estimate in portfolio.json's
    `premium_paid`. The two can diverge widely on options because the
    agent's training-data priors are often 5-10× off real market premiums;
    dashboards / P&L code should prefer this when computing realised vs
    intended-vs-actual P&L.

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
