"""Alpaca activities → state/trades.jsonl sync.

Phase 2 of the per-trade PnL build. PR #52 added the trades.jsonl log
and FIFO matcher; this module supplies the data that flows into the log
by pulling real fills + fee activities from Alpaca's REST API.

Idempotency: every row in trades.jsonl has an ``activity_id`` from
Alpaca; before writing we check ``state.read_trade_activity_ids()`` and
skip anything already on disk. Safe to call repeatedly (e.g. after
every orchestrator cycle).

Fee handling: Alpaca returns fees (REG, TAF, etc.) as SEPARATE
activities from their parent fills. We pull both pools in one sync,
match fees to fills by ``order_id``, and SPLIT each order's total fee
proportionally by qty across the (possibly multiple) FILL activities
sharing that order_id — Alpaca emits one FILL per execution
(``type=partial_fill`` for partials), so a single order can produce
several FILL rows that must each carry their share of the fee. Codex
P1 on PR #55 caught the pre-fix bug where every partial fill received
the FULL order fee. For paper equity trading Alpaca currently reports
$0 fees; live USD-funded accounts pick up the SEC/TAF schedule.

Run-id attribution: the optional ``order_id_to_run_id`` map lets the
caller stamp the run_id of the orchestrator cycle that submitted each
parent order. Fills with no map entry land with ``run_id=None`` so
``lib.trades.compute_trades_pnl`` correctly assigns them zero LLM
attribution (manual / out-of-band trades).

This is the only module besides ``lib.alpaca_client`` that imports
alpaca-py. The live broker is Alpaca (CLAUDE.md §Critical preconditions #2),
so going live needs no change here — live fills sync through the same Alpaca
client, and live Alpaca's real SEC/TAF fees populate ``fees_usd`` automatically
(paper reports $0).
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from . import state


# Alpaca's activities API paginates with a hard max of 100 rows per page.
# We loop until a page returns fewer than this OR until we see no new IDs,
# whichever comes first. The cap below is a safety rail against an upstream
# pagination bug (or a stub test) returning the same page forever.
_PAGE_SIZE = 100
_MAX_PAGES = 200   # 20k rows; far above any plausible single-sync volume


# Alpaca activity types that represent fees we want to fold into a fill's
# fees_usd. Per the public docs (2026-Q2 snapshot):
#   REG / SEC — SEC fee on sells
#   TAF — Trading Activity Fee (FINRA)
#   FINRA_TAF — FINRA TAF on sells
#   FEE — generic Alpaca fee bucket
# Equities paper has no commission; the regulatory schedule (SEC/TAF) still
# applies on live USD-funded accounts.
#
# Empirical probe (2026-05-26, paper account): Alpaca paper rejects every
# category-specific type with HTTP 400 code 40010001 "invalid activity
# type". Only `FEE` is accepted on paper. Live (USD-funded) accounts may
# still expose the granular types — Alpaca's docs imply they're
# account-class-dependent. We list all of them and rely on
# _is_unsupported_activity_type_error to swallow the 400s on accounts
# that only expose FEE. Codex P1 on PR #89: without listing FEE we
# would silently write every fill with fees_usd=0 on paper, distorting
# realized PnL — even though on paper fees are nominally $0, real-fee
# bookkeeping should still flow through the same code path.
_FEE_ACTIVITY_TYPES: tuple[str, ...] = (
    "FEE",
    "REG", "SEC", "TAF", "FINRA_TAF",
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
    """Return 'etf' for every fill — the system is ETF-only.

    The ``kind`` field is retained on trade rows for schema/back-compat,
    but it is always ``etf``: there is no option instrument class.
    """
    return "etf"


def _normalize_side(side: Any) -> str:
    """Map Alpaca side values to our 'buy'/'sell' canonical form.

    Alpaca returns 'buy' or 'sell' on FILL activities. We map anything
    that isn't 'buy' to 'sell' for safety on this long-only paper account.
    """
    s = str(side).lower()
    return "buy" if s == "buy" else "sell"


def _is_unsupported_activity_type_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` represents Alpaca's "this account / API
    doesn't support that activity type" — the only fee-endpoint failure
    mode we want to silently skip. Anything else (auth, 5xx, rate limit,
    parse error) must surface so the operator notices instead of writing
    fills with silently-wrong fees_usd=0.

    Two manifestations from the real Alpaca paper API (probed 2026-05-26):
      - **404 Not Found** — the historical contract: "this account doesn't
        have this activity type" (e.g. TAF on an account that never
        traded equities). Documented in alpaca-py.
      - **Code 40010001 "invalid activity type: X"** — the newer
        contract: the paper API rejects every fee category except `FEE`.
        Observed verbatim for REG, SEC, TAF, FINRA_TAF on a fresh paper
        account.

    Without the 40010001 branch, the very first iteration of the fee-pull
    loop raises and kills `sync_fills_from_alpaca` entirely — no fills
    ever land in trades.jsonl, the Trades tab shows 0, the Performance
    tab's realized line stays flat. This caused a multi-week silent
    blackout on the VPS deploy (May 22 → May 26).

    Why we don't gate on ``status_code == 400`` (PR #90, observed on the
    deployed fix): alpaca-py's ``APIError`` is version-dependent — sometimes
    it attaches ``.status_code``, sometimes ``.response.status_code``,
    sometimes neither (the body is the only signal). The first VPS resync
    after PR #89 merged still hit the propagated exception even though
    the code path was correct, because ``status_code`` was None and the
    400-gated branch didn't engage. We now require BOTH signals from the
    exception's stringified body — the `40010001` code AND the literal
    "invalid activity type" message phrase — and accept the match
    regardless of how the SDK happens to surface the status. Codex P2's
    concern (don't swallow other 40010001 validation failures) is still
    addressed because the message-phrase half of the AND only matches
    the documented unsupported-activity-type response.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status == 404:
        return True
    # The combination of code AND phrase is the documented Alpaca
    # 'invalid activity type' response. Don't gate on status_code —
    # alpaca-py SDK versions vary in whether they attach it. The two
    # message-body signals together are tight enough on their own.
    msg = str(exc)
    return "40010001" in msg and "invalid activity type" in msg.lower()


# Back-compat alias — the old name was misleadingly 404-specific.
# Keep it pointing at the new helper so external callers / tests don't
# break if anyone imported it directly.
_is_unsupported_activity_404 = _is_unsupported_activity_type_error


def _paginate_activities(
    trading_client: Any, *, activity_type: str, after: str | None,
) -> list[dict]:
    """Walk every page of ``/account/activities/{activity_type}``.

    Codex P2 on PR #55: Alpaca's activities endpoint caps a single response
    at 100 rows. Without pagination, syncs after a high-volume period (or
    a long offline window) silently drop the older fills past row 100,
    leaving permanent gaps in trades.jsonl.

    Strategy: request ``page_size=100``; if the response is full (== 100),
    use the LAST row's ``id`` as the next ``page_token`` and re-request.
    Loop until a short page or empty response. Safety-capped at
    ``_MAX_PAGES`` so a buggy upstream can't spin us.

    Stub tests can either:
      - return a single non-full list (one page → loop exits naturally), or
      - implement ``page_token``-aware paging in their fake client.
    """
    all_rows: list[dict] = []
    page_token: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"page_size": _PAGE_SIZE}
        if after:
            params["after"] = after
        if page_token:
            params["page_token"] = page_token
        chunk = trading_client.get(f"/account/activities/{activity_type}", params)
        if not isinstance(chunk, list) or not chunk:
            break
        all_rows.extend(chunk)
        if len(chunk) < _PAGE_SIZE:
            break  # short page = last page
        # Alpaca's pagination uses the last row's id as the next token.
        last_id = chunk[-1].get("id")
        if not last_id or last_id == page_token:
            # Defensive: if upstream is misbehaving and not advancing the
            # cursor, exit rather than loop forever.
            break
        page_token = last_id
    return all_rows


def _build_fee_index(fee_rows: list[dict]) -> dict[str, float]:
    """Sum ``net_amount`` (absolute value) per ``order_id`` across fee activities.

    Alpaca reports fees as DEBITS — negative ``net_amount``. We flip to
    positive USD so the downstream ``fees_usd`` column reads as a positive
    cost. Multiple fee activities on a single order are summed into one value.
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

    # ---- Pull FILL activities (paginated) ----
    fill_rows = _paginate_activities(
        trading_client, activity_type="FILL", after=after,
    )

    # ---- Pull fee activities (paginated per fee type) ----
    # Codex P1 on PR #55: narrow exception handling. Catch only 404
    # (== "this account doesn't have this activity type"); let
    # auth / 5xx / rate-limit / parse errors propagate so the operator
    # sees a real failure instead of fills being silently written with
    # fees_usd=0.
    fee_rows: list[dict] = []
    for ftype in _FEE_ACTIVITY_TYPES:
        try:
            fee_rows.extend(_paginate_activities(
                trading_client, activity_type=ftype, after=after,
            ))
        except Exception as e:
            if _is_unsupported_activity_type_error(e):
                continue
            raise

    fee_index = _build_fee_index(fee_rows)

    # ---- Plan new fills and split fees pro-rata by qty (Codex P1) ----
    # Group new (unseen) fills by order_id so we can compute each fill's
    # share of the order-level fee. A single order can produce multiple
    # FILL activities (partial_fill); assigning the full fee to each
    # would double-count.
    known_ids = state.read_trade_activity_ids()
    order_id_to_run_id = order_id_to_run_id or {}

    new_fills_by_order: dict[str, list[dict]] = defaultdict(list)
    seen_this_call: set[str] = set()
    for row in fill_rows:
        aid = row.get("id")
        if not aid or aid in known_ids:
            continue
        if aid in seen_this_call:
            # Same activity_id appeared twice in fill_rows — happens when an
            # upstream pagination bug returns the same page repeatedly. The
            # _paginate_activities guard exits the loop quickly but the
            # already-consumed duplicate page is still in fill_rows; skip it
            # here so trades.jsonl gets one row per fill, not multiple.
            continue
        seen_this_call.add(aid)
        oid = row.get("order_id") or ""
        new_fills_by_order[oid].append(row)

    # ---- Append new fills with pro-rata fees ----
    written = 0
    fees_matched = 0
    fills_written_orders: set[str] = set()
    for oid, rows in new_fills_by_order.items():
        order_fee_total = fee_index.get(oid, 0.0)
        # Sum qty across the NEW fills for this order so the split
        # denominator is consistent. Caveat: if part of this order
        # already has fills on disk (from a prior sync window) and fees
        # for that order are also arriving now, this sync's split will
        # over-allocate fees to NEW fills. That partial-fill-across-sync
        # case is rare in practice (orders fill in seconds) and is
        # explicitly the territory of the reconcile-fees pass flagged
        # for PR #58 / Phase 2.5.
        total_qty = sum(_to_float(r.get("qty")) for r in rows) or 0.0
        for row in rows:
            aid = row.get("id")
            symbol = row.get("symbol") or ""
            qty = _to_float(row.get("qty"))
            if total_qty > 0 and order_fee_total > 0:
                fee_share = order_fee_total * (qty / total_qty)
            else:
                fee_share = 0.0
            if fee_share > 0:
                fees_matched += 1
            state.append_trade({
                "activity_id": aid,
                "alpaca_order_id": oid,
                "symbol": symbol,
                "kind": _normalize_kind(symbol),
                "side": _normalize_side(row.get("side")),
                "qty": qty,
                "fill_price": _to_float(row.get("price")),
                "fees_usd": fee_share,
                "filled_at": row.get("transaction_time") or "",
                "run_id": order_id_to_run_id.get(oid),
            })
            written += 1
            fills_written_orders.add(oid)

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


def order_id_to_run_id_from_runs() -> dict[str, str]:
    """Build the {broker_order_id: run_id} map from per-run orders.json files.

    The orchestrator writes ``state/runs/{run_id}/orders.json`` after every
    execute stage that submits orders. Each file lists the order_ids
    Alpaca accepted in that cycle. This helper walks every run dir,
    reads each orders.json, and builds the union map.

    Used by the activities sync to stamp each fill's ``run_id`` so
    ``lib/trades.compute_trades_pnl`` can attribute LLM cost per the
    locked methodology (per-position equal split of the opening run's
    cost). Order_ids missing from the map land as ``run_id=None`` — those
    are treated as manual operator trades with zero LLM attribution.
    """
    import json as _json
    out: dict[str, str] = {}
    runs_dir = state.RUNS_DIR
    if not runs_dir.exists():
        return out
    for run_dir in runs_dir.iterdir():
        orders_path = run_dir / "orders.json"
        if not orders_path.exists():
            continue
        try:
            data = _json.loads(orders_path.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            continue
        rid = data.get("run_id") or run_dir.name
        for oid in data.get("order_ids") or []:
            if oid:
                out[oid] = rid
    return out


# Backwards-compat alias so callers from PR #55 era still resolve. The
# function it pointed at always returned {} (it was a stub flagged as
# "lands in follow-up PR" — this PR is the follow-up).
order_id_to_run_id_from_decisions = order_id_to_run_id_from_runs
