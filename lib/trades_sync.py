"""Alpaca activities → state/trades.jsonl sync.

Phase 2 of the per-trade PnL build. PR #52 added the trades.jsonl log
and FIFO matcher; this module supplies the data that flows into the log
by pulling real fills + fee activities from Alpaca's REST API.

Idempotency: every row in trades.jsonl has an ``activity_id`` from
Alpaca; before writing we check ``state.read_trade_activity_ids()`` and
skip anything already on disk. Safe to call repeatedly (e.g. after
every orchestrator cycle).

Fee handling: Alpaca returns fees (OCC, REG, TAF, etc.) as SEPARATE
activities from their parent fills. We pull both pools in one sync,
match fees to fills by ``order_id``, sum into ``fees_usd``, and emit
one trade row per fill. Fees that arrive in a later sync window — e.g.
overnight clearing-house adjustments — get folded into a follow-up
``reconcile_fees`` pass. For paper equity trading Alpaca currently
reports $0 fees; option contracts pick up the OCC/SEC schedule.

Run-id attribution: the optional ``order_id_to_run_id`` map lets the
caller stamp the run_id of the orchestrator cycle that submitted each
parent order. Fills with no map entry land with ``run_id=None`` so
``lib.trades.compute_trades_pnl`` correctly assigns them zero LLM
attribution (manual / out-of-band trades).

This is the only module besides ``lib.alpaca_client`` and
``lib.options_chain`` that imports alpaca-py — the IBKR swap path
(CLAUDE.md §Critical preconditions #2) stays a one-file change with
analogous helpers behind the same signature.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from . import state


# Alpaca activity types that represent fees we want to fold into a fill's
# fees_usd. Per the public docs (2026-Q2 snapshot):
#   OCC — Option Clearing Corporation fee (~$0.05/contract)
#   ORF — Options Regulatory Fee
#   REG / SEC — SEC fee on sells
#   TAF — Trading Activity Fee (FINRA)
#   FINRA_TAF — FINRA TAF on sells
# Equities paper has no commission; options paper charges OCC/REG schedule.
_FEE_ACTIVITY_TYPES: tuple[str, ...] = (
    "OCC", "ORF", "REG", "SEC", "TAF", "FINRA_TAF",
)


@dataclass(frozen=True)
class SyncResult:
    """Return value of sync_fills_from_alpaca."""
    new_fills_written: int
    fills_seen: int           # total FILL activities returned by Alpaca
    fees_matched: int         # number of fee activities folded into fills
    fees_unmatched: int       # fees whose order_id didn't map to a fill we wrote


def _to_float(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalize_kind(symbol: str) -> str:
    """Return 'option' for OSI-shaped symbols, 'etf' otherwise.

    We can't ask Alpaca activities directly — the response only carries
    the OCC OSI string. ETF symbols are 1-5 uppercase letters with no
    digits; an OSI is ≥ 16 chars containing the date+strike encoding.
    The 15-char floor is conservative (shortest 1-char underlying OSI is
    1 + 6 + 1 + 8 = 16 chars).
    """
    if len(symbol) >= 16 and any(c.isdigit() for c in symbol):
        return "option"
    return "etf"


def _normalize_side(side: Any) -> str:
    """Map Alpaca side values to our 'buy'/'sell' canonical form.

    Alpaca returns 'buy' or 'sell' on FILL activities. Option fill activities
    can also return 'sell_short' for opening a short (not in scope on this
    long-only paper account); we map them all to 'sell' for safety.
    """
    s = str(side).lower()
    return "buy" if s == "buy" else "sell"


def _build_fee_index(fee_rows: list[dict]) -> dict[str, float]:
    """Sum ``net_amount`` (absolute value) per ``order_id`` across fee activities.

    Alpaca reports fees as DEBITS — negative ``net_amount``. We flip to
    positive USD so the downstream ``fees_usd`` column reads as a positive
    cost. Fees on a single order are added together so a 2-leg option
    spread with two OCC fees lands as one summed value.
    """
    out: dict[str, float] = defaultdict(float)
    for row in fee_rows:
        oid = row.get("order_id")
        if not oid:
            continue
        amt = abs(_to_float(row.get("net_amount") or row.get("amount") or 0))
        out[oid] += amt
    return dict(out)


def sync_fills_from_alpaca(
    *,
    trading_client: Any | None = None,
    order_id_to_run_id: dict[str, str] | None = None,
    after: str | None = None,
) -> SyncResult:
    """Pull FILL + fee activities from Alpaca, append new fills to trades.jsonl.

    Args:
        trading_client: alpaca-py ``TradingClient`` (or a stub with a ``.get``
            method for tests). When None, constructs an AlpacaBroker just
            for credentials — same env-var contract as the rest of the
            codebase (ALPACA_API_KEY / ALPACA_API_SECRET).
        order_id_to_run_id: optional map ``{alpaca_order_id: run_id}`` so
            each new fill row gets stamped with the run that submitted
            its parent order. Missing entries → ``run_id=None`` (treated
            as a manual operator trade by ``lib.trades``).
        after: optional ISO UTC timestamp; only activities at or after this
            are requested. When None, pulls Alpaca's default window. Use
            this when you want to sync recent activity without re-paging
            through months of history.

    Returns: SyncResult with counts. Always idempotent — re-running the
    same window writes nothing if all activity_ids are already on disk.
    """
    if trading_client is None:
        from .alpaca_client import AlpacaBroker
        # AlpacaBroker exposes its internal TradingClient as ._client.
        broker = AlpacaBroker()
        trading_client = broker._client  # noqa: SLF001

    # ---- Pull FILL activities ----
    params: dict = {}
    if after:
        params["after"] = after
    fill_rows = trading_client.get("/account/activities/FILL", params) or []

    # ---- Pull fee activities (each type is a separate endpoint) ----
    fee_rows: list[dict] = []
    for ftype in _FEE_ACTIVITY_TYPES:
        try:
            chunk = trading_client.get(f"/account/activities/{ftype}", params) or []
        except Exception:
            # Some fee types may not exist on a given account — Alpaca
            # returns 404 on unknown activity_type. Skip rather than fail
            # the whole sync; missing fees just mean fees_usd=0 for now.
            chunk = []
        if isinstance(chunk, list):
            fee_rows.extend(chunk)

    fee_index = _build_fee_index(fee_rows)

    # ---- Append new fills idempotently ----
    known_ids = state.read_trade_activity_ids()
    written = 0
    fees_matched = 0
    order_id_to_run_id = order_id_to_run_id or {}

    for row in fill_rows:
        aid = row.get("id")
        if not aid or aid in known_ids:
            continue
        symbol = row.get("symbol") or ""
        order_id = row.get("order_id") or ""
        fee_for_order = fee_index.get(order_id, 0.0)
        if fee_for_order > 0:
            fees_matched += 1
        state.append_trade({
            "activity_id": aid,
            "alpaca_order_id": order_id,
            "symbol": symbol,
            "kind": _normalize_kind(symbol),
            "side": _normalize_side(row.get("side")),
            "qty": _to_float(row.get("qty")),
            "fill_price": _to_float(row.get("price")),
            "fees_usd": fee_for_order,
            "filled_at": row.get("transaction_time") or "",
            "run_id": order_id_to_run_id.get(order_id),
        })
        written += 1

    # Fees that didn't match anything we wrote — could be from an older
    # fill already in trades.jsonl. PR #58 will add a separate reconcile
    # pass that updates existing rows with late-arriving fees. For now
    # report the count so the operator can see whether reconciliation is
    # worth wiring up immediately.
    fills_written_orders = {
        row.get("order_id") for row in fill_rows if row.get("id") not in known_ids
    }
    fees_unmatched = sum(
        1 for oid in fee_index
        if oid not in fills_written_orders
    )

    return SyncResult(
        new_fills_written=written,
        fills_seen=len(fill_rows),
        fees_matched=fees_matched,
        fees_unmatched=fees_unmatched,
    )


def order_id_to_run_id_from_decisions() -> dict[str, str]:
    """Best-effort map from Alpaca order_id → run_id, built from past
    execute-stage decision log entries.

    Today the execute stage doesn't record broker_order_id alongside the
    submission (PR #46 wired the broker in but doesn't surface the order
    id back into the decision log payload). Until that lands this map
    will be empty for current/historical runs and new fills will
    attribute as run_id=None.

    PR after this should add ``broker_order_id`` to ``next_run["order_plan"]
    ["results"]`` so this helper has data to work with.
    """
    out: dict[str, str] = {}
    if not state.DECISIONS_LOG.exists():
        return out
    import json as _json
    for line in state.DECISIONS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if d.get("stage") != "execute":
            continue
        rid = d.get("run_id")
        # The decision log row points at output_ref="next_run.json"; we
        # could load that file and read its order_plan but the file gets
        # overwritten each run. The right home for this map is the per-run
        # state/runs/{run_id}/orders.json — that lands in the follow-up PR.
        _ = rid  # placeholder until follow-up PR
    return out
