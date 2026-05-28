"""Tests for orchestrator._account_nav — synthetic-NAV sizing (Phase 3).

Alpaca paper accounts ship with $100k of fake equity. CLAUDE.md sizes for a
$2,500 experimental account, so the agent sizes against the SYNTHETIC balance
($2,500 baseline, VIRTUAL_NAV_USD-overridable, + realized P&L) and NEVER the
broker's ~$100k equity. Phase 3 removed the broker-equity fallback entirely so
a missing/garbled env var can't make the agent size ~40× too large.

These tests use tmp_state so the synthetic balance reads empty logs (→ baseline).
"""
from __future__ import annotations

import orchestrator
from lib import state
from lib.broker import Account


class _FakeBroker:
    def __init__(self, equity_usd: float):
        self._equity = equity_usd

    @property
    def name(self): return "fake"

    def get_account(self):
        return Account(
            cash_usd=self._equity, equity_usd=self._equity,
            buying_power_usd=self._equity * 2, is_paper=True,
        )

    def get_positions(self): return []
    def submit_order(self, *a, **kw): raise NotImplementedError
    def cancel_all(self): return 0
    def flatten(self, sym): return None


def _ctx(broker=None) -> orchestrator.StageContext:
    return orchestrator.StageContext(run_id="t", dry_run=True, broker=broker)


def test_account_nav_uses_virtual_override_as_baseline(tmp_state, monkeypatch):
    """VIRTUAL_NAV_USD is the synthetic baseline; with no trades, NAV == it,
    regardless of broker-reported equity."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 2500.0


def test_account_nav_override_baseline_even_for_low_value(tmp_state, monkeypatch):
    monkeypatch.setenv("VIRTUAL_NAV_USD", "1500.50")
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 1500.50


def test_account_nav_never_uses_broker_equity(tmp_state, monkeypatch):
    """Phase 3: broker equity ($100k paper) must NEVER leak into sizing, even
    with no VIRTUAL_NAV_USD set. Falls to the $2,500 synthetic baseline."""
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 2500.0


def test_account_nav_falls_back_to_baseline_when_no_broker(tmp_state, monkeypatch):
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    assert orchestrator._account_nav(_ctx(None)) == 2500.0


def test_account_nav_ignores_unparseable_override(tmp_state, monkeypatch):
    """Garbage override must not crash — fall through to the $2,500 baseline."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "not-a-number")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    assert orchestrator._account_nav(_ctx(None)) == 2500.0


def test_account_nav_tracks_realized_pnl(tmp_state, monkeypatch):
    """Phase 3: a banked round-trip lifts the synthetic NAV the agent sizes
    against. Buy TQQQ ×4 @ $70, sell @ $80 → +$40 realized → NAV $2,540."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 4, "fill_price": 70.0,
        "fees_usd": 0.0, "filled_at": "2026-05-28T14:00:00Z",
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 4, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-28T15:00:00Z",
    })
    assert orchestrator._account_nav(_ctx(None)) == 2540.0


def test_account_nav_uses_raw_llm_cost_ignoring_display_reset(tmp_state, monkeypatch):
    """Codex P2 (#98): sizing NAV must use the RAW cost audit log, so clicking
    'Reset all LLM costs' (a display-only marker) does not inflate the capital
    the agent sizes against."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    state.append_cost({
        "run_id": "r", "stage": "construct", "model": "m",
        "cost_usd": 50.0, "at": state.utcnow_iso(),
    })
    assert orchestrator._account_nav(_ctx(None)) == 2450.0  # 2500 − 50 raw cost
    state.set_all_time_cost_reset()  # hide costs in the dashboard display
    assert orchestrator._account_nav(_ctx(None)) == 2450.0  # unchanged — raw, not display


def test_parsed_virtual_nav_override_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    assert orchestrator._parsed_virtual_nav_override() is None


def test_parsed_virtual_nav_override_returns_none_when_garbage(monkeypatch):
    """Codex P1 on PR #76: misconfigured env var ('not-a-number',
    empty string after parsing) must return None so the orchestrator
    stamps nav_source='broker' on the row rather than mis-flagging
    a broker-units row as virtual."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "not-a-number")
    assert orchestrator._parsed_virtual_nav_override() is None


def test_parsed_virtual_nav_override_returns_float_when_valid(monkeypatch):
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500.0")
    assert orchestrator._parsed_virtual_nav_override() == 2500.0


def test_parsed_virtual_nav_override_handles_whitespace_negative(monkeypatch):
    """Edge cases: leading/trailing whitespace parses fine, negative
    numbers also parse (operator might want to stress-test a negative
    NAV scenario). The helper just reports parseability."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "  -500  ")
    assert orchestrator._parsed_virtual_nav_override() == -500.0
