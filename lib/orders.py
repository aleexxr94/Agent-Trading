"""Order reconciliation — diff target portfolio vs current broker positions
and emit the OrderRequest list that converges actual → target.

Gated behind ORDERS_ENABLED=true. Orchestrator.stage_execute imports this
and only submits orders when the env flag is on; default-off until the
operator opts in.

Scope:
  - ETF orders: integer-share market orders (Phase 10c).
  - Option orders: OSI-symbol market orders for opening + closing legs
    (Phase 10d). Single legs only — no multi-leg combos yet (spreads,
    straddles etc. would need a separate request shape).

OSI symbol convention used (matches Alpaca paper):
    {UNDERLYING}{YYMMDD}{C|P}{STRIKE*1000:08d}
  e.g.  SPY  call, $530 strike, 2026-06-19  →  SPY260619C00530000
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from .broker import Broker, BrokerPosition, OrderRequest, OrderResult

# OCC OSI option-symbol shape: 1-6 char underlying + YYMMDD + C/P + strike*1000
# zero-padded to 8 digits. Used to detect option legs in submit_plan without
# threading kind="option" through OrderRequest (which would muddle the ETF path).
_OSI_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def is_osi_symbol(symbol: str) -> bool:
    """True iff ``symbol`` matches the OCC OSI option-symbol shape."""
    return bool(_OSI_RE.match(symbol))


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


def osi_symbol(*, underlying: str, expiry: str, type: str, strike: float) -> str:
    """Build an OCC OSI-format option symbol that Alpaca accepts.

    Args:
        underlying: e.g. "SPY"
        expiry: ISO date "YYYY-MM-DD"
        type: "call" or "put"
        strike: strike price in USD, e.g. 530.0 or 437.5

    Returns: e.g. "SPY260619C00530000"
    """
    if type not in ("call", "put"):
        raise ValueError(f"option type must be 'call' or 'put', got {type!r}")
    yymmdd = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
    cp = "C" if type == "call" else "P"
    # Strike encoding: strike × 1000, zero-padded to 8 digits. $530.00 → 530000.
    strike_int = int(round(strike * 1000))
    if strike_int < 0 or strike_int > 99_999_999:
        raise ValueError(f"strike {strike} out of OSI range")
    return f"{underlying}{yymmdd}{cp}{strike_int:08d}"


def _osi_for_target_option(option_pos: dict) -> str:
    return osi_symbol(
        underlying=option_pos["underlying"],
        expiry=option_pos["expiry"],
        type=option_pos["type"],
        strike=option_pos["strike"],
    )


def _current_etf_qty(positions: list[BrokerPosition]) -> dict[str, float]:
    return {
        p.symbol: p.qty for p in positions if p.asset_class == "us_equity"
    }


def _current_option_qty(positions: list[BrokerPosition]) -> dict[str, float]:
    """Map OSI-symbol → contract qty for option holdings."""
    return {
        p.symbol: p.qty for p in positions if p.asset_class == "us_option"
    }


def _plan_for_symbol(
    *, symbol: str, current_qty: float, target_qty: float,
) -> tuple[list[OrderRequest], list[OrderRequest]]:
    """Compute (closes, opens) for a single symbol with the no-cross-zero
    invariant.

    Invariant: a single OrderRequest never causes the broker's position
    to cross zero in one ticket. If current and target have opposite
    signs (e.g. currently long 4, target short 2), the path is split:
    first close the existing position (sell 4 → flat), then open the
    opposite (sell 2 short). Two tickets, never one.

    For v2's long-only schema this never triggers — the constructor
    can't produce a short target. But the invariant is here as a
    defensive rail in case the schema ever gains shorts, AND as a
    correctness contract on the close-before-open ordering (closes
    list is submitted first by submit_plan, so cash is freed before
    opens consume buying power).

    Returns (closes, opens) where each list contains 0-1 OrderRequest.
    Combined, the net effect is: broker holds exactly target_qty after
    all orders fill.
    """
    if current_qty == target_qty:
        return [], []

    # Same-sign or one-side-zero: a single delta order suffices.
    if current_qty == 0:
        # 0 → target: pure open.
        side = "buy" if target_qty > 0 else "sell"
        return [], [OrderRequest(symbol=symbol, qty=abs(target_qty), side=side, order_type="market")]
    if target_qty == 0:
        # current → 0: pure close.
        side = "sell" if current_qty > 0 else "buy"
        return [OrderRequest(symbol=symbol, qty=abs(current_qty), side=side, order_type="market")], []
    if (current_qty > 0) == (target_qty > 0):
        # Same sign: single delta order. abs(delta) is correct size and
        # the side depends on whether we're growing or shrinking the
        # position.
        delta = target_qty - current_qty
        side = "buy" if (delta > 0 and current_qty > 0) or (delta < 0 and current_qty < 0) else "sell"
        return [], [OrderRequest(symbol=symbol, qty=abs(delta), side=side, order_type="market")]

    # Opposite signs — the dangerous case. Split into close + open.
    # Step 1: close current (sell if long, buy-to-cover if short).
    close_side = "sell" if current_qty > 0 else "buy"
    close = OrderRequest(symbol=symbol, qty=abs(current_qty), side=close_side, order_type="market")
    # Step 2: open opposite-sign target.
    open_side = "buy" if target_qty > 0 else "sell"
    opn = OrderRequest(symbol=symbol, qty=abs(target_qty), side=open_side, order_type="market")
    return [close], [opn]


def diff_portfolio(target_portfolio: dict, broker_positions: list[BrokerPosition]) -> OrderPlan:
    """Compare target portfolio with broker's current positions; return
    an OrderPlan that closes-before-opens AND never crosses zero in a
    single ticket. See ``_plan_for_symbol`` for the invariant.

    For v2's long-only schema the no-cross-zero rail never triggers,
    but it stays here as a defensive contract.
    """
    requests: list[OrderRequest] = []
    closes: list[OrderRequest] = []
    skipped: list[dict] = []

    # ---- ETFs ----
    target_etfs = {
        p["symbol"]: p["shares"]
        for p in target_portfolio.get("positions", [])
        if p["kind"] == "etf"
    }
    current_etfs = _current_etf_qty(broker_positions)

    for sym in sorted(set(target_etfs) | set(current_etfs)):
        target_qty = target_etfs.get(sym, 0)
        current_qty = current_etfs.get(sym, 0)
        sym_closes, sym_opens = _plan_for_symbol(
            symbol=sym, current_qty=current_qty, target_qty=target_qty,
        )
        closes.extend(sym_closes)
        requests.extend(sym_opens)

    # ---- Options ----
    target_options: dict[str, int] = {}
    for p in target_portfolio.get("positions", []):
        if p["kind"] != "option":
            continue
        try:
            osi = _osi_for_target_option(p)
        except (KeyError, ValueError) as e:
            skipped.append({
                "underlying": p.get("underlying"), "type": p.get("type"),
                "strike": p.get("strike"), "expiry": p.get("expiry"),
                "contracts": p.get("contracts"),
                "reason": f"could not build OSI symbol: {e}",
            })
            continue
        target_options[osi] = p["contracts"]

    current_options = _current_option_qty(broker_positions)

    for osi in sorted(set(target_options) | set(current_options)):
        target_qty = target_options.get(osi, 0)
        current_qty = current_options.get(osi, 0)
        osi_closes, osi_opens = _plan_for_symbol(
            symbol=osi, current_qty=current_qty, target_qty=target_qty,
        )
        closes.extend(osi_closes)
        requests.extend(osi_opens)

    return OrderPlan(requests=requests, skipped=skipped, closes=closes)


def _alpaca_error_label(e: Exception) -> str:
    """Build a diagnostic label from an Alpaca API error.

    Per Alpaca's error spec, REST 4xx/5xx responses carry structured
    {"code": <int>, "message": "..."} bodies. The alpaca-py SDK surfaces
    these as APIError exceptions with .code / .message attributes. When
    they're present we render a short, log-friendly label like
        error[40010001]: time_in_force must be valid
    so the operator can pinpoint cause without grep'ing the SDK source.

    Falls back to the generic <TypeName>: <repr> shape for everything
    else (network errors, mocked test exceptions, etc.).
    """
    code = getattr(e, "code", None)
    message = getattr(e, "message", None)
    if code is not None and message:
        return f"error[{code}]: {message}"
    if message:
        return f"error: {type(e).__name__}: {message}"
    return f"error: {type(e).__name__}: {e}"


def submit_plan(plan: OrderPlan, *, broker: Broker) -> list[OrderResult]:
    """Submit every OrderRequest in the plan via the broker, in close → open
    order (frees cash before sizing new positions). Returns the result list
    so the orchestrator can log broker order IDs into the decision log.

    Option-open guard: before submitting a BUY for an OSI-shaped symbol,
    ask the broker whether the contract exists and is tradable. If not,
    skip with a clear status string instead of submitting an order that
    will fail with Alpaca's generic error[42210000] 'asset not found'.
    Closes (sells) are never gated — if we hold the position the contract
    obviously exists at the broker.
    """
    results: list[OrderResult] = []
    for req in plan.closes + plan.requests:
        if req.side == "buy" and is_osi_symbol(req.symbol):
            if not broker.option_contract_tradable(req.symbol):
                results.append(OrderResult(
                    broker_order_id="",
                    symbol=req.symbol,
                    qty=req.qty,
                    side=req.side,
                    submitted_at="",
                    status="skipped: option contract not tradable at broker",
                ))
                continue
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
                status=_alpaca_error_label(e),
            ))
    return results
