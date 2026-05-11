"""Tests for orchestrator._account_nav — VIRTUAL_NAV_USD override.

Alpaca paper accounts ship with $100k of fake equity. CLAUDE.md sizes for a
$2,500 experimental account, so the agent must size against the smaller
virtual NAV rather than the broker-reported equity when the operator sets
the override. Falls back to broker equity, then to the $2,500 baseline.
"""
from __future__ import annotations

import orchestrator
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


def test_account_nav_uses_virtual_override_when_set(monkeypatch):
    """VIRTUAL_NAV_USD pins NAV regardless of broker-reported equity."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 2500.0


def test_account_nav_override_wins_over_broker_even_for_zero(monkeypatch):
    """Override must take precedence — operator could legitimately want $0
    to test all-cash behaviour without disconnecting the broker."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "1500.50")
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 1500.50


def test_account_nav_falls_back_to_broker_when_no_override(monkeypatch):
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    broker = _FakeBroker(equity_usd=4242.0)
    assert orchestrator._account_nav(_ctx(broker)) == 4242.0


def test_account_nav_falls_back_to_baseline_when_no_broker(monkeypatch):
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    assert orchestrator._account_nav(_ctx(None)) == 2500.0


def test_account_nav_ignores_unparseable_override(monkeypatch):
    """Garbage override must not crash — fall through to broker / baseline."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "not-a-number")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    assert orchestrator._account_nav(_ctx(None)) == 2500.0


def test_account_nav_override_used_when_broker_raises(monkeypatch):
    """If the broker call errors, the override should still win cleanly."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")

    class _BrokenBroker(_FakeBroker):
        def get_account(self): raise RuntimeError("network down")

    broker = _BrokenBroker(equity_usd=0)
    assert orchestrator._account_nav(_ctx(broker)) == 2500.0
