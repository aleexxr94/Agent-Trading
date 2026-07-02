"""Live-mode sizing NAV — shared by orchestrator (position sizing, NAV
history) and monitor (8% daily-drawdown breaker) so the two can never
denominate in different scales.

The allocated NAV is the capital the agent is allowed to size against on a
genuinely live broker:

  - no cap configured: real ``Account.equity_usd``
  - ``LIVE_NAV_CAP_USD`` set: ``min(starting_equity, cap) + (equity -
    starting_equity)`` — the capped starting allocation, debited/credited by
    live P&L since the transition (Codex P1 on PR #112: a hard
    ``min(equity, cap)`` stays pinned at the cap through losses while the
    account is funded above it, so adaptive caps and the DD breaker would
    never see a drawdown in the agent's actual risk budget). The result can
    never exceed real equity; profits compound the allocation just like the
    paper synthetic balance compounds realized wins.

``starting_equity`` comes from the write-once ``state/live_transition.json``
marker, recorded here on the first successful live equity read. Known
limitation: deposits/withdrawals after the transition shift the
``equity - starting_equity`` delta — re-anchor by clearing the marker (a
deliberate operator action) if the account is ever re-funded.

Fail-closed contract: ANY problem — broker/equity read failure, a paper
account on the live path, non-finite or non-positive equity, a malformed
cap, or an allocation debited to zero — raises ``LiveNavUnavailable``.
There is deliberately NO fallback to the paper/synthetic baseline here.
"""
from __future__ import annotations

import os

from . import live_gate, state


class LiveNavUnavailable(RuntimeError):
    """Live broker equity could not be read or validated. The live sizing
    path NEVER falls back to the synthetic/$2,500 baseline — sizing a real
    account against the wrong scale is worse than skipping the cycle, so
    callers must fail closed (no orders, no LLM spend, retry later)."""


def broker_is_live(broker) -> bool:
    """True iff the broker object is a genuinely live (non-paper) client.
    Constructing one already requires the triple lock (AlpacaBroker refuses
    non-paper without LIVE_TRADING_ENABLED=true); a broker without an
    is_paper attribute is treated as paper."""
    return broker is not None and getattr(broker, "is_paper", True) is False


def live_nav_cap_usd() -> float | None:
    """Optional operator ceiling on the live starting allocation
    (LIVE_NAV_CAP_USD). Unset/blank → no cap. A malformed or non-positive
    value raises LiveNavUnavailable: a typo'd cap must halt the cycle
    loudly, never silently size against the full deposit."""
    raw = os.environ.get("LIVE_NAV_CAP_USD")
    if raw is None or not raw.strip():
        return None
    try:
        cap = float(raw)
    except ValueError:
        raise LiveNavUnavailable(f"LIVE_NAV_CAP_USD is not a number: {raw!r}")
    if not (cap > 0) or cap != cap or cap == float("inf"):
        raise LiveNavUnavailable(
            f"LIVE_NAV_CAP_USD must be a positive finite USD amount: {raw!r}"
        )
    return cap


def live_allocated_nav(broker, *, run_id: str | None = None) -> float:
    """The NAV the agent may size against on a live broker (see module
    docstring for the allocation formula). Raises LiveNavUnavailable on ANY
    problem. On the first successful read it records the write-once
    paper→live transition marker with the real starting equity."""
    cap = live_nav_cap_usd()
    if not broker_is_live(broker):
        raise LiveNavUnavailable(
            "live env lock is fully raised but no live broker is available "
            "(broker construction failed or a paper broker was passed)"
        )
    try:
        account = broker.get_account()
    except Exception as e:
        raise LiveNavUnavailable(f"broker.get_account failed: {type(e).__name__}: {e}")
    if getattr(account, "is_paper", True):
        raise LiveNavUnavailable("broker reports a paper account on the live NAV path")
    try:
        equity = float(account.equity_usd)
    except (TypeError, ValueError, AttributeError) as e:
        raise LiveNavUnavailable(f"live equity unreadable: {type(e).__name__}: {e}")
    if not (equity > 0) or equity != equity or equity == float("inf"):
        raise LiveNavUnavailable(f"live equity invalid: {equity!r}")
    try:
        marker = state.write_live_transition_once(
            live_starting_equity_usd=equity,
            nav_cap_usd=cap,
            run_id=run_id,
            live_version=live_gate.LIVE_VERSION,
        )
    except Exception:
        marker = None  # marker is telemetry; recording must never block sizing
    if cap is None:
        return equity
    start = equity
    if isinstance(marker, dict) and isinstance(
        marker.get("live_starting_equity_usd"), (int, float)
    ):
        start = float(marker["live_starting_equity_usd"])
    nav = min(start, cap) + (equity - start)
    if not (nav > 0):
        raise LiveNavUnavailable(
            f"live allocated NAV exhausted: start={start}, cap={cap}, "
            f"equity={equity} → allocation {nav:.2f} ≤ 0. Operator action "
            "required (review losses; re-anchor by clearing "
            "state/live_transition.json after re-funding)."
        )
    return nav
