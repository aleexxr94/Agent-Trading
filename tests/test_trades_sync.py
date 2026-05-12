"""Alpaca activities → trades.jsonl sync tests.

Strictly: a stub TradingClient with a ``.get()`` method that returns
canned FILL + fee activity rows. No alpaca-py dependency at test time —
the real SDK gets wrapped behind the same interface in production.
"""
from __future__ import annotations

import pytest

from lib import state, trades_sync


class _FakeTrading:
    """Stub TradingClient. ``responses`` is a dict {path: list[dict]}.

    Unknown paths return [] so the sync's fee-pull loop can iterate every
    fee type without each one needing an explicit entry. Tracks calls so
    tests can assert which endpoints were hit."""

    def __init__(self, responses: dict[str, list[dict]] | None = None,
                 raise_on: tuple[str, ...] = ()):
        self.responses = responses or {}
        self.raise_on = set(raise_on)
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None):
        self.calls.append((path, params or {}))
        if path in self.raise_on:
            raise RuntimeError(f"simulated 404 on {path}")
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
