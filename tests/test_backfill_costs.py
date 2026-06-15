"""Legacy cost backfill tests (bin/backfill_costs.py)."""
from __future__ import annotations

import json

import pytest

from bin import backfill_costs
from lib import state


def _legacy(activity_id, *, order_id, side, qty, price, fees_usd=0.0):
    """A pre-cost-model row: no fee_source / slippage_usd fields."""
    return {
        "activity_id": activity_id, "alpaca_order_id": order_id,
        "symbol": "SQQQ", "kind": "etf", "side": side, "qty": qty,
        "fill_price": price, "fees_usd": fees_usd,
        "filled_at": "2026-05-01T13:00:00Z", "run_id": None,
    }


def test_backfill_rows_tags_legacy_with_modelled_costs():
    rows = [
        _legacy("b1", order_id="o1", side="buy", qty=500, price=20.0),
        _legacy("s1", order_id="o2", side="sell", qty=500, price=20.0),
    ]
    new_rows, changed = backfill_costs.backfill_rows(rows)
    assert changed == 2
    buy, sell = new_rows
    # Buy: $0 fee, slippage > 0, modelled.
    assert buy["fee_source"] == "modelled"
    assert buy["fees_usd"] == 0.0
    assert buy["slippage_usd"] > 0.0
    # Sell: modelled regulatory fee + slippage.
    assert sell["fee_source"] == "modelled"
    assert sell["fees_usd"] == pytest.approx(0.31, abs=0.01)
    assert sell["slippage_usd"] > 0.0


def test_backfill_rows_preserves_real_fees():
    rows = [_legacy("s1", order_id="o1", side="sell", qty=500, price=20.0, fees_usd=0.50)]
    new_rows, _ = backfill_costs.backfill_rows(rows)
    assert new_rows[0]["fee_source"] == "real"
    assert new_rows[0]["fees_usd"] == 0.50          # untouched
    assert new_rows[0]["slippage_usd"] > 0.0        # slippage still added


def test_backfill_rows_idempotent_skips_tagged():
    tagged = _legacy("s1", order_id="o1", side="sell", qty=500, price=20.0)
    tagged["fee_source"] = "modelled"
    tagged["slippage_usd"] = 2.0
    tagged["fees_usd"] = 0.31
    new_rows, changed = backfill_costs.backfill_rows([dict(tagged)])
    assert changed == 0
    assert new_rows[0] == tagged                    # untouched


def test_backfill_main_writes_and_is_idempotent(tmp_state):
    for r in (
        _legacy("b1", order_id="o1", side="buy", qty=500, price=20.0),
        _legacy("s1", order_id="o2", side="sell", qty=500, price=20.0),
    ):
        state.append_trade(r)
    # First run rewrites the log + backs up.
    assert backfill_costs.main([]) == 0
    backup = state.TRADES_LOG.with_suffix(state.TRADES_LOG.suffix + ".bak")
    assert backup.exists()
    rows = [json.loads(x) for x in state.TRADES_LOG.read_text().splitlines() if x.strip()]
    assert all("fee_source" in r and "slippage_usd" in r for r in rows)
    # Second run is a no-op (all rows now tagged).
    before = state.TRADES_LOG.read_text()
    assert backfill_costs.main([]) == 0
    assert state.TRADES_LOG.read_text() == before


def test_backfill_dry_run_writes_nothing(tmp_state):
    state.append_trade(_legacy("s1", order_id="o1", side="sell", qty=500, price=20.0))
    before = state.TRADES_LOG.read_text()
    assert backfill_costs.main(["--dry-run"]) == 0
    assert state.TRADES_LOG.read_text() == before


def test_backfill_refuses_when_cost_model_inactive(tmp_state, monkeypatch):
    monkeypatch.setenv("PAPER_COST_MODEL", "false")
    state.append_trade(_legacy("s1", order_id="o1", side="sell", qty=500, price=20.0))
    assert backfill_costs.main([]) == 2              # refuses, exit code 2
