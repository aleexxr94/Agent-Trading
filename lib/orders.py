"""Order reconciliation — diff target portfolio vs current broker positions
and emit the OrderRequest list that converges actual → target.

Gated behind ORDERS_ENABLED=true. Orchestrator.stage_execute imports this
and only submits orders when the env flag is on; default-off until the
operator opts in.

Scope:
  - ETF orders: integer-share market orders.

The system is ETF-only. Any option-shaped target (kind="option", an OSI
symbol, or option-only fields like contracts/strike/expiry/premium_paid) is
rejected defensively before it can reach the broker — it is never submitted.

Hard universe guard (fail-closed): the tradable set is exactly the symbols
in lib/universe.py. Any non-universe symbol — a typo, a plain index ETF
(SPY/QQQ), a single name (TSLA) — is refused at the order boundary, both when
planning opens (diff_portfolio) and at submission (submit_plan), for BOTH buys
and sells. Universe membership being enforced only by the upstream signals
filter + constructor prompt was advisory; this is the real gate.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import universe
from .broker import Broker, BrokerPosition, OrderRequest, OrderResult

# OCC OSI option-symbol shape: 1-6 char underlying + YYMMDD + C/P + strike*1000
# zero-padded to 8 digits. Retained ONLY as a defensive detector so an option
# symbol can never be submitted as an order. The system does not build these.
_OSI_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")

# Position fields that only exist on options — their presence flags a payload
# that must be rejected before reaching the broker.
_OPTION_ONLY_FIELDS = ("contracts", "strike", "expiry", "premium_paid", "greeks", "underlying")


def is_osi_symbol(symbol: str) -> bool:
    """True iff ``symbol`` matches the OCC OSI option-symbol shape."""
    return bool(_OSI_RE.match(symbol or ""))


# The exact tradable universe. Built once from lib/universe.py; the order
# layer refuses anything outside it. Symbols are upper-case (schema-enforced).
_UNIVERSE_SYMBOLS: frozenset[str] = frozenset(universe.all_symbols())

# Reason strings (shared so tests can assert on a stable phrase). Symbol count
# is derived from the universe so the message can't rot when the universe grows.
_NOT_IN_UNIVERSE_SKIP_REASON = (
    f"symbol not in ETF-only universe ({len(_UNIVERSE_SYMBOLS)} symbols)"
)
_NOT_IN_UNIVERSE_ORDER_STATUS = f"skipped: {_NOT_IN_UNIVERSE_SKIP_REASON}"


def in_universe(symbol: str) -> bool:
    """True iff ``symbol`` is one of the tradable ETF tickers in lib/universe.py."""
    return symbol in _UNIVERSE_SYMBOLS


def is_tradable_target(position: dict) -> bool:
    """True iff this target position is one the order layer would actually
    submit: ETF-only (not option-shaped) AND in the tradable universe. The
    inverse of what diff_portfolio drops into ``skipped``. Used to keep
    current_portfolio.json from recording a position that can never be held.
    """
    return not _is_option_like(position) and in_universe(position.get("symbol") or "")


def _is_option_like(position: dict) -> bool:
    """Defensive: detect an option-shaped target position so the order layer
    can refuse it. ETF-only system — this should never fire in normal
    operation, but it fails closed if an option payload ever slips through."""
    if position.get("kind") == "option":
        return True
    if is_osi_symbol(position.get("symbol") or ""):
        return True
    return any(position.get(f) is not None for f in _OPTION_ONLY_FIELDS)


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
        # Same sign (both long, or both short — current schema is long-only
        # but the helper stays defensive). Single delta order.
        #
        # Broker semantics: a `buy` always adds to position (grows a long
        # or covers a short toward zero); a `sell` always reduces a long
        # toward zero OR builds a short. So with current and target on
        # the same side of zero, the order side is determined purely by
        # the sign of (target - current):
        #
        #   long  4 → long  6: delta=+2 → buy  (grow long)
        #   long  6 → long  4: delta=-2 → sell (reduce long)
        #   short-5 → short-2: delta=+3 → buy  (cover toward zero)
        #   short-2 → short-5: delta=-3 → sell (grow short)
        #
        # Codex P1 on the v2 PR: an earlier version inverted this for
        # negative current_qty (buy⇄sell flipped on short adjustments).
        # The simple rule above is the correct one.
        delta = target_qty - current_qty
        side = "buy" if delta > 0 else "sell"
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
    target_etfs: dict[str, float] = {}
    for p in target_portfolio.get("positions", []):
        # Defensive reject: options are not a supported instrument class.
        # Any option-shaped payload is dropped here and never submitted.
        if _is_option_like(p):
            skipped.append({
                "symbol": p.get("symbol") or p.get("underlying"),
                "kind": p.get("kind"),
                "reason": "option payloads are not supported (ETF-only system)",
            })
            continue
        # Hard universe guard (fail-closed): never plan an open for a symbol
        # outside the tradable ETF universe — a typo or a non-universe ETF
        # (SPY/QQQ/TSLA) is dropped here rather than sized into a position.
        if not in_universe(p["symbol"]):
            skipped.append({
                "symbol": p["symbol"],
                "kind": p.get("kind"),
                "reason": _NOT_IN_UNIVERSE_SKIP_REASON,
            })
            continue
        target_etfs[p["symbol"]] = p["shares"]

    current_etfs = _current_etf_qty(broker_positions)

    for sym in sorted(set(target_etfs) | set(current_etfs)):
        target_qty = target_etfs.get(sym, 0)
        current_qty = current_etfs.get(sym, 0)
        sym_closes, sym_opens = _plan_for_symbol(
            symbol=sym, current_qty=current_qty, target_qty=target_qty,
        )
        closes.extend(sym_closes)
        requests.extend(sym_opens)

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

    Defensive option guard (fail-closed): the system is ETF-only and never
    builds option symbols, but if an OSI-shaped symbol ever reaches here it
    is refused outright rather than submitted to the broker.

    Hard universe guard (fail-closed): any symbol outside the tradable ETF
    universe is refused here too — for BOTH buys and sells. This is the true
    broker boundary (it also sees closes), so a non-universe symbol can never
    reach broker.submit_order regardless of how the plan was built. Exiting a
    stray/legacy non-universe holding is handled by monitor.py flatten or a
    manual action, NOT this order path.
    """
    results: list[OrderResult] = []
    for req in plan.closes + plan.requests:
        if is_osi_symbol(req.symbol):
            results.append(OrderResult(
                broker_order_id="",
                symbol=req.symbol,
                qty=req.qty,
                side=req.side,
                submitted_at="",
                status="skipped: option symbols are not supported (ETF-only system)",
            ))
            continue
        if not in_universe(req.symbol):
            results.append(OrderResult(
                broker_order_id="",
                symbol=req.symbol,
                qty=req.qty,
                side=req.side,
                submitted_at="",
                status=_NOT_IN_UNIVERSE_ORDER_STATUS,
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
