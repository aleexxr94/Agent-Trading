"""Order reconciliation — diff target portfolio vs current broker positions
and emit the OrderRequest list that converges actual → target.

Gated behind ORDERS_ENABLED=true. Orchestrator.stage_execute imports this
and only submits orders when the env flag is on; default-off until the
operator opts in.

Scope (Phase 10c):
  - ETF orders: integer-share market orders.
  - Option orders: deliberately SKIPPED — options orders need an OSI symbol
    (e.g. SPY261219C00530000) plus contract qty + leg semantics. That
    lands in Phase 10d once we wire a quote helper and a contract-OSI
    builder. For now options positions in the target portfolio are surfaced
    as a `skipped_reason` row in the diff output so the orchestrator can
    flag them in the decision log.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .broker import Broker, BrokerPosition, OrderRequest, OrderResult


def is_enabled() -> bool:
    """Read ORDERS_ENABLED from env. Default OFF; operator opts in explicitly."""
    return os.environ.get("ORDERS_ENABLED", "false").lower() == "true"


@dataclass(frozen=True)
class OrderPlan:
    requests: list[OrderRequest] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    closes: list[OrderRequest] = field(default_factory=list)

    @property
    def total_legs(self) -> int:
        return len(self.requests) + len(self.closes)


def _current_etf_qty(positions: list[BrokerPosition]) -> dict[str, float]:
    return {
        p.symbol: p.qty for p in positions if p.asset_class == "us_equity"
    }


def diff_portfolio(target_portfolio: dict, broker_positions: list[BrokerPosition]) -> OrderPlan:
    """Compare target portfolio (output of stage_construct) with what the
    broker actually holds; return an OrderPlan.

    Algorithm:
      1. For each ETF in target, compare to current qty. Submit a buy/sell
         to converge to the target qty.
      2. For each ETF in broker positions but NOT in target, submit a sell
         for the full quantity (full close).
      3. For each option in target, append a skipped row (Phase 10d will
         handle these).
    """
    requests: list[OrderRequest] = []
    closes: list[OrderRequest] = []
    skipped: list[dict] = []

    target_etfs = {
        p["symbol"]: p["shares"]
        for p in target_portfolio.get("positions", [])
        if p["kind"] == "etf"
    }
    current = _current_etf_qty(broker_positions)

    # 1. converge held → target, and 2. close anything not in target
    all_symbols = set(target_etfs) | set(current)
    for sym in sorted(all_symbols):
        target_qty = target_etfs.get(sym, 0)
        current_qty = current.get(sym, 0)
        delta = target_qty - current_qty
        if delta == 0:
            continue
        if target_qty == 0:
            # full close
            closes.append(OrderRequest(
                symbol=sym, qty=abs(current_qty),
                side="sell" if current_qty > 0 else "buy",
                order_type="market",
            ))
        elif delta > 0:
            # buy more
            requests.append(OrderRequest(
                symbol=sym, qty=abs(delta), side="buy", order_type="market",
            ))
        else:
            # trim down
            requests.append(OrderRequest(
                symbol=sym, qty=abs(delta), side="sell", order_type="market",
            ))

    # 3. Options — deferred to Phase 10d
    for p in target_portfolio.get("positions", []):
        if p["kind"] == "option":
            skipped.append({
                "underlying": p["underlying"], "type": p["type"],
                "strike": p["strike"], "expiry": p["expiry"],
                "contracts": p["contracts"],
                "reason": "options ordering deferred to Phase 10d",
            })

    return OrderPlan(requests=requests, skipped=skipped, closes=closes)


def submit_plan(plan: OrderPlan, *, broker: Broker) -> list[OrderResult]:
    """Submit every OrderRequest in the plan via the broker, in close → open
    order (frees cash before sizing new positions). Returns the result list
    so the orchestrator can log broker order IDs into the decision log."""
    results: list[OrderResult] = []
    for req in plan.closes + plan.requests:
        try:
            results.append(broker.submit_order(req))
        except Exception as e:
            # Don't break the rest of the plan on one bad order; log into
            # results as a synthetic failure row so the caller sees it.
            results.append(OrderResult(
                broker_order_id="",
                symbol=req.symbol,
                qty=req.qty,
                side=req.side,
                submitted_at="",
                status=f"error: {type(e).__name__}: {e}",
            ))
    return results
