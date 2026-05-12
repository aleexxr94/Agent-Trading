"""Alpaca activities → trades.jsonl sync tests.

Strictly: a stub TradingClient with a ``.get()`` method that returns
canned FILL + fee activity rows. No alpaca-py dependency at test time —
the real SDK gets wrapped behind the same interface in production.
"""
from __future__ import annotations

import pytest

from lib import state, trades_sync


class _Alpaca404(Exception):
    """Mimics alpaca-py's APIError for an unsupported-activity-type 404."""
    def __init__(self, msg: str = "activity_type not found"):
        super().__init__(msg)
        self.status_code = 404


class _Alpaca500(Exception):
    """Mimics alpaca-py's APIError for a transient server failure."""
    def __init__(self, msg: str = "server error"):
        super().__init__(msg)
        self.status_code = 500


class _FakeTrading:
    """Stub TradingClient. ``responses`` is a dict {path: list[dict]}.

    Unknown paths return [] so the sync's fee-pull loop can iterate every
    fee type without each one needing an explicit entry. Tracks calls so
    tests can assert which endpoints were hit.

    ``raise_on`` maps path → exception instance. Defaults raise a
    404-shaped exception so the legacy "skip unknown activity type"
    tests still pass; pass a 5xx-shaped exception explicitly to verify
    error propagation.
    """

    def __init__(self, responses: dict[str, list[dict]] | None = None,
                 raise_on: dict[str, BaseException] | tuple[str, ...] = ()):
        self.responses = responses or {}
        if isinstance(raise_on, tuple):
            # Back-compat: paths in a tuple raise a default 404.
            self.raise_on = {p: _Alpaca404() for p in raise_on}
        else:
            self.raise_on = dict(raise_on)
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None):
        self.calls.append((path, params or {}))
        if path in self.raise_on:
            raise self.raise_on[path]
        return self.responses.get(path, [])


def _fill(activity_id="20260512000000::a1", *, order_id="ord-1", symbol="TQQQ",
          side="buy", qty="10", price="70.00",
          transaction_time="2026-05-12T14:30:00Z") -> dict:
    return {
        "id": activity_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "transaction_time": transaction_time,
        "activity_type": "FILL",
    }


def _fee(*, order_id, net_amount="-0.65", activity_type="OCC"):
    """Fee activities arrive with NEGATIVE net_amount (it's a debit)."""
    return {
        "id": f"fee-{order_id}-{activity_type}",
        "order_id": order_id,
        "net_amount": net_amount,
        "activity_type": activity_type,
        "transaction_time": "2026-05-12T14:30:01Z",
    }


# ---------- happy path ----------


def test_sync_appends_new_fill_with_zero_fees_for_etf(tmp_state):
    """Equity FILL with no matching fee activities → fees_usd=0."""
    tc = _FakeTrading(responses={"/account/activities/FILL": [_fill()]})
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 1
    assert res.fills_seen == 1

    rows = state.read_trades()
    assert len(rows) == 1
    r = rows[0]
    assert r["activity_id"] == "20260512000000::a1"
    assert r["alpaca_order_id"] == "ord-1"
    assert r["symbol"] == "TQQQ"
    assert r["kind"] == "etf"
    assert r["side"] == "buy"
    assert r["qty"] == 10.0
    assert r["fill_price"] == 70.0
    assert r["fees_usd"] == 0.0
    assert r["filled_at"] == "2026-05-12T14:30:00Z"
    assert r["run_id"] is None  # no order_id_to_run_id map passed


def test_sync_folds_occ_fee_into_option_fill(tmp_state):
    """OSI-shaped symbol → kind=option; fee activity with matching order_id
    is folded into fees_usd as a POSITIVE value (Alpaca reports debits as
    negative net_amount)."""
    osi = "SPY260619P00510000"
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [_fill(
            activity_id="fill-1", order_id="ord-opt-1", symbol=osi,
            qty="1", price="0.61",
        )],
        "/account/activities/OCC": [_fee(order_id="ord-opt-1", net_amount="-0.65")],
    })
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 1
    assert res.fees_matched == 1

    r = state.read_trades()[0]
    assert r["kind"] == "option"
    assert r["fees_usd"] == pytest.approx(0.65)  # flipped to positive


def test_sync_sums_multiple_fee_types_for_same_order(tmp_state):
    """OCC + REG + TAF fees on a single fill should sum into fees_usd."""
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [_fill(order_id="ord-X")],
        "/account/activities/OCC": [_fee(order_id="ord-X", net_amount="-0.05")],
        "/account/activities/REG": [_fee(order_id="ord-X", net_amount="-0.03",
                                          activity_type="REG")],
        "/account/activities/TAF": [_fee(order_id="ord-X", net_amount="-0.02",
                                          activity_type="TAF")],
    })
    trades_sync.sync_fills_from_alpaca(trading_client=tc)
    r = state.read_trades()[0]
    assert r["fees_usd"] == pytest.approx(0.10)


# ---------- idempotency ----------


def test_sync_skips_activity_ids_already_in_log(tmp_state):
    """Second sync with the same activity_id does not duplicate the row."""
    tc = _FakeTrading(responses={"/account/activities/FILL": [_fill()]})
    r1 = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    r2 = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert r1.new_fills_written == 1
    assert r2.new_fills_written == 0
    assert len(state.read_trades()) == 1


def test_sync_appends_only_new_fills(tmp_state):
    """First sync writes 1; second sync (with a second fill present) writes 1 more."""
    fill1 = _fill(activity_id="a1", order_id="o1")
    fill2 = _fill(activity_id="a2", order_id="o2", symbol="SOXL",
                   transaction_time="2026-05-13T09:00:00Z")
    tc = _FakeTrading(responses={"/account/activities/FILL": [fill1]})
    trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert len(state.read_trades()) == 1

    # Add second fill; second sync should pick up only it
    tc.responses["/account/activities/FILL"] = [fill1, fill2]
    r2 = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert r2.new_fills_written == 1
    rows = state.read_trades()
    assert [r["activity_id"] for r in rows] == ["a1", "a2"]


# ---------- run-id attribution ----------


def test_sync_stamps_run_id_from_map(tmp_state):
    """When order_id_to_run_id is supplied, each fill's run_id is the
    map entry for its order_id. Map miss → run_id=None."""
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [
            _fill(activity_id="a1", order_id="ord-tracked"),
            _fill(activity_id="a2", order_id="ord-manual", symbol="SOXL"),
        ],
    })
    trades_sync.sync_fills_from_alpaca(
        trading_client=tc,
        order_id_to_run_id={"ord-tracked": "run-A"},
    )
    rows = {r["activity_id"]: r for r in state.read_trades()}
    assert rows["a1"]["run_id"] == "run-A"
    assert rows["a2"]["run_id"] is None  # not in the map → manual fill


# ---------- after-window param ----------


def test_sync_passes_after_param_to_alpaca(tmp_state):
    tc = _FakeTrading(responses={"/account/activities/FILL": []})
    trades_sync.sync_fills_from_alpaca(
        trading_client=tc, after="2026-05-12T00:00:00Z",
    )
    # First call hits FILL endpoint with the `after` param
    fill_call = next(c for c in tc.calls if c[0] == "/account/activities/FILL")
    assert fill_call[1].get("after") == "2026-05-12T00:00:00Z"


# ---------- defensive: missing / weird Alpaca shapes ----------


def test_sync_handles_unknown_fee_endpoint_gracefully(tmp_state):
    """Some Alpaca activity types 404 on accounts that don't use them.
    sync_fills_from_alpaca must absorb the per-type exception and continue
    with the next fee type rather than aborting the whole sync."""
    tc = _FakeTrading(
        responses={"/account/activities/FILL": [_fill()]},
        raise_on=("/account/activities/REG", "/account/activities/TAF"),
    )
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 1
    # FILL endpoint + every fee endpoint attempted (some raised, sync survived)
    paths = {c[0] for c in tc.calls}
    assert "/account/activities/FILL" in paths
    assert "/account/activities/OCC" in paths
    assert "/account/activities/REG" in paths


def test_sync_skips_fill_with_no_activity_id(tmp_state):
    """Malformed fill row (missing id) shouldn't crash the sync."""
    bad = {**_fill(), "id": None}
    tc = _FakeTrading(responses={"/account/activities/FILL": [bad]})
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 0
    assert state.read_trades() == []


def test_sync_normalises_string_price_qty_to_float(tmp_state):
    """Alpaca returns numbers as strings; the writer needs floats."""
    fill = _fill(price="0.6100", qty="2")
    tc = _FakeTrading(responses={"/account/activities/FILL": [fill]})
    trades_sync.sync_fills_from_alpaca(trading_client=tc)
    r = state.read_trades()[0]
    assert isinstance(r["fill_price"], float) and r["fill_price"] == pytest.approx(0.61)
    assert isinstance(r["qty"], float) and r["qty"] == 2.0


def test_sync_reports_unmatched_fees(tmp_state):
    """Fee activities whose order_id doesn't match any fill we wrote in
    this sync window are reported via SyncResult.fees_unmatched so the
    operator can decide whether to wire up the reconcile pass."""
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [_fill(order_id="ord-1")],
        "/account/activities/OCC": [
            _fee(order_id="ord-1"),                    # matched
            _fee(order_id="ord-stale"),                # belongs to a fill not in this window
        ],
    })
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.fees_matched == 1
    assert res.fees_unmatched == 1


# ---------- side normalisation ----------


@pytest.mark.parametrize("alpaca_side,expected", [
    ("buy", "buy"),
    ("sell", "sell"),
    ("sell_short", "sell"),  # we treat shorts as sells (long-only paper account)
    ("Buy", "buy"),
])
def test_sync_normalises_side(tmp_state, alpaca_side, expected):
    fill = _fill(side=alpaca_side)
    tc = _FakeTrading(responses={"/account/activities/FILL": [fill]})
    trades_sync.sync_fills_from_alpaca(trading_client=tc)
    r = state.read_trades()[0]
    assert r["side"] == expected


# ---------- Codex P1 (PR #55): fees split across partial fills ----------


def test_sync_splits_order_fees_pro_rata_across_partial_fills(tmp_state):
    """Single order, two FILL activities (partial fills 4 + 6 of 10 contracts),
    one OCC fee activity at $1.00 for the order. Each fill must carry its
    pro-rata share — NOT the full $1.00 each (the pre-fix bug Codex caught).
    """
    osi = "SPY260619P00510000"
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [
            _fill(activity_id="fill-1", order_id="ord-X", symbol=osi,
                  qty="4", price="0.61"),
            _fill(activity_id="fill-2", order_id="ord-X", symbol=osi,
                  qty="6", price="0.61"),
        ],
        "/account/activities/OCC": [_fee(order_id="ord-X", net_amount="-1.00")],
    })
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 2
    assert res.fees_matched == 2  # both fills picked up a non-zero share
    rows = {r["activity_id"]: r for r in state.read_trades()}
    # 4/10 of $1 = $0.40, 6/10 of $1 = $0.60. Sum must equal the order fee.
    assert rows["fill-1"]["fees_usd"] == pytest.approx(0.40)
    assert rows["fill-2"]["fees_usd"] == pytest.approx(0.60)
    assert (rows["fill-1"]["fees_usd"] + rows["fill-2"]["fees_usd"]) == pytest.approx(1.00)


def test_sync_single_fill_receives_entire_order_fee(tmp_state):
    """Edge case: only one fill for the order → it gets 100% of the fee.
    The pro-rata math (qty/total_qty = 1.0) must produce the same answer
    as the pre-fix code did for non-partial orders."""
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [_fill(order_id="ord-Y", symbol="SPY260619P00510000")],
        "/account/activities/OCC": [_fee(order_id="ord-Y", net_amount="-0.65")],
    })
    trades_sync.sync_fills_from_alpaca(trading_client=tc)
    r = state.read_trades()[0]
    assert r["fees_usd"] == pytest.approx(0.65)


# ---------- Codex P1 (PR #55): exception handling narrowed to 404 ----------


def test_sync_skips_only_404_on_fee_endpoint(tmp_state):
    """Unsupported activity types return 404 — must be skipped silently.
    A FILL with no matching fee in any other endpoint lands with fees_usd=0."""
    tc = _FakeTrading(
        responses={"/account/activities/FILL": [_fill()]},
        raise_on={"/account/activities/REG": _Alpaca404(),
                  "/account/activities/TAF": _Alpaca404()},
    )
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 1
    assert state.read_trades()[0]["fees_usd"] == 0.0


def test_sync_propagates_5xx_on_fee_endpoint(tmp_state):
    """500 / auth / rate-limit on a fee endpoint must NOT be swallowed —
    silently writing fills with fees_usd=0 hides real data corruption.
    Codex P1: narrow the catch."""
    tc = _FakeTrading(
        responses={"/account/activities/FILL": [_fill()]},
        raise_on={"/account/activities/OCC": _Alpaca500()},
    )
    with pytest.raises(_Alpaca500):
        trades_sync.sync_fills_from_alpaca(trading_client=tc)


def test_sync_propagates_unexpected_exception(tmp_state):
    """Anything that's not a 404-shaped exception must propagate so the
    operator sees the failure instead of writing zero-fee fills."""
    tc = _FakeTrading(
        responses={"/account/activities/FILL": [_fill()]},
        raise_on={"/account/activities/OCC": RuntimeError("parse error")},
    )
    with pytest.raises(RuntimeError):
        trades_sync.sync_fills_from_alpaca(trading_client=tc)


def test_sync_uses_status_via_response_attribute(tmp_state):
    """httpx-style errors carry status via .response.status_code, not .status_code.
    The predicate must check both shapes."""
    class _HTTPXLikeError(Exception):
        def __init__(self):
            super().__init__("Not Found")
            self.response = type("R", (), {"status_code": 404})()
    tc = _FakeTrading(
        responses={"/account/activities/FILL": [_fill()]},
        raise_on={"/account/activities/OCC": _HTTPXLikeError()},
    )
    # 404 detected via .response.status_code → skipped silently
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == 1


# ---------- Codex P2 (PR #55): pagination ----------


class _PaginatedFakeTrading:
    """Stub that honours ``page_token`` so we can test the pagination loop.

    ``pages`` is a dict mapping path → list-of-pages. Each call consumes
    the next page based on the ``page_token`` query param. Alpaca returns
    last row's ``id`` as the next token, mimicked here.
    """

    def __init__(self, pages: dict[str, list[list[dict]]]):
        self.pages = pages
        # Map (path, page_token or '') → page-index for deterministic lookup.
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None):
        params = params or {}
        self.calls.append((path, params))
        pages = self.pages.get(path, [])
        token = params.get("page_token")
        if not token:
            return pages[0] if pages else []
        # Find the page whose first row's predecessor (last id of previous page)
        # equals `token`.
        for i in range(1, len(pages)):
            prev_last_id = pages[i - 1][-1].get("id")
            if prev_last_id == token:
                return pages[i]
        return []


def test_sync_paginates_through_all_fill_pages(tmp_state):
    """A FILL response that fills the page_size cap must trigger a follow-up
    request with page_token = last row's id. Loop continues until a short
    page. Codex P2: without pagination, syncs after a high-volume period
    drop the older fills past row 100."""
    # Build TWO pages: first page exactly _PAGE_SIZE rows, second page short.
    page_size = trades_sync._PAGE_SIZE
    page_1 = [_fill(activity_id=f"a{i:04d}", order_id=f"o{i}") for i in range(page_size)]
    page_2 = [_fill(activity_id="aLast", order_id="oLast")]
    tc = _PaginatedFakeTrading(pages={
        "/account/activities/FILL": [page_1, page_2],
    })
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    assert res.new_fills_written == page_size + 1
    # The second FILL request must have carried page_token=last id of page 1.
    fill_calls = [c for c in tc.calls if c[0] == "/account/activities/FILL"]
    assert len(fill_calls) == 2
    assert fill_calls[1][1].get("page_token") == page_1[-1]["id"]


def test_sync_short_page_does_not_request_next_page(tmp_state):
    """If the first page has fewer than _PAGE_SIZE rows, no follow-up
    request — saves a needless round-trip."""
    tc = _FakeTrading(responses={"/account/activities/FILL": [_fill()]})
    trades_sync.sync_fills_from_alpaca(trading_client=tc)
    fill_calls = [c for c in tc.calls if c[0] == "/account/activities/FILL"]
    assert len(fill_calls) == 1


def test_sync_stops_paginating_when_cursor_does_not_advance(tmp_state):
    """Defensive: if upstream is broken and returns the same page forever,
    the loop must exit rather than spin. We exit when the new page_token
    equals the previous one."""
    page_size = trades_sync._PAGE_SIZE
    page = [_fill(activity_id=f"a{i:04d}", order_id=f"o{i}") for i in range(page_size)]

    class _StuckTrading:
        def __init__(self):
            self.calls = 0
        def get(self, path: str, params: dict | None = None):
            self.calls += 1
            if path == "/account/activities/FILL":
                return page  # always the same page, ignoring page_token
            return []
    tc = _StuckTrading()
    res = trades_sync.sync_fills_from_alpaca(trading_client=tc)
    # First call returns the page; second call returns it again with the same
    # last_id so the loop exits. Written count is page_size (first batch).
    assert res.new_fills_written == page_size
    assert tc.calls <= 3 + len(trades_sync._FEE_ACTIVITY_TYPES) * 2, (
        "loop must exit quickly on stuck cursor, not run _MAX_PAGES times"
    )


# ---------- order_id_to_run_id_from_runs (PR 3 wiring) ----------


def test_order_id_to_run_id_from_runs_empty_when_no_runs(tmp_state):
    """No state/runs/ entries → empty map. Callers stamp run_id=None."""
    assert trades_sync.order_id_to_run_id_from_runs() == {}


def test_order_id_to_run_id_from_runs_aggregates_across_run_dirs(tmp_state):
    """Each state/runs/{run_id}/orders.json contributes its order_ids to
    the map. Union across all runs; collisions resolved by latest write
    on disk (irrelevant in practice — Alpaca order_ids are globally unique)."""
    import json as _json
    for rid, oids in [
        ("run-A", ["ord-1", "ord-2"]),
        ("run-B", ["ord-3"]),
    ]:
        d = state.RUNS_DIR / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "orders.json").write_text(_json.dumps({
            "run_id": rid, "submitted_at": "2026-05-12T08:00:00Z",
            "order_ids": oids,
        }))
    out = trades_sync.order_id_to_run_id_from_runs()
    assert out == {"ord-1": "run-A", "ord-2": "run-A", "ord-3": "run-B"}


def test_order_id_to_run_id_from_runs_skips_malformed_json(tmp_state):
    """A broken orders.json file mustn't crash the helper — skip it and
    continue to the next run dir."""
    import json as _json
    good_dir = state.RUNS_DIR / "run-A"
    good_dir.mkdir(parents=True, exist_ok=True)
    (good_dir / "orders.json").write_text(_json.dumps({
        "run_id": "run-A", "submitted_at": "x", "order_ids": ["ord-1"],
    }))
    bad_dir = state.RUNS_DIR / "run-B"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "orders.json").write_text("{not valid json")
    out = trades_sync.order_id_to_run_id_from_runs()
    assert out == {"ord-1": "run-A"}


def test_order_id_to_run_id_from_runs_skips_empty_order_id_entries(tmp_state):
    import json as _json
    d = state.RUNS_DIR / "run-A"
    d.mkdir(parents=True, exist_ok=True)
    (d / "orders.json").write_text(_json.dumps({
        "run_id": "run-A", "submitted_at": "x",
        "order_ids": ["ord-1", "", None, "ord-2"],
    }))
    out = trades_sync.order_id_to_run_id_from_runs()
    assert out == {"ord-1": "run-A", "ord-2": "run-A"}


def test_sync_uses_run_id_map_built_from_runs_dir(tmp_state):
    """End-to-end wiring: orchestrator writes orders.json per cycle;
    activities sync builds the map from those files and stamps the
    run_id onto each new fill row."""
    import json as _json
    d = state.RUNS_DIR / "run-A"
    d.mkdir(parents=True, exist_ok=True)
    (d / "orders.json").write_text(_json.dumps({
        "run_id": "run-A", "submitted_at": "x", "order_ids": ["ord-tracked"],
    }))
    tc = _FakeTrading(responses={
        "/account/activities/FILL": [
            _fill(activity_id="a1", order_id="ord-tracked"),
            _fill(activity_id="a2", order_id="ord-manual", symbol="SOXL"),
        ],
    })
    trades_sync.sync_fills_from_alpaca(
        trading_client=tc,
        order_id_to_run_id=trades_sync.order_id_to_run_id_from_runs(),
    )
    rows = {r["activity_id"]: r for r in state.read_trades()}
    assert rows["a1"]["run_id"] == "run-A"
    assert rows["a2"]["run_id"] is None  # manual fill, no run dir for ord-manual
