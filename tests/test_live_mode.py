"""trading_mode — paper/live derivation for record mode-tagging.

The env-only path must never claim "live" unless the FULL triple lock is
raised (env flag + LIVE_VERSION bumped + non-paper base URL); a broker
object's ``is_paper`` is authoritative when available. Any ambiguity
resolves to "paper".
"""
from __future__ import annotations

from lib import live_gate
from lib.alpaca_client import PAPER_BASE_URL, AlpacaBroker


class _BrokerStub:
    def __init__(self, is_paper):
        if is_paper is not None:
            self.is_paper = is_paper


def test_default_env_is_paper(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    assert live_gate.trading_mode() == "paper"


def test_env_true_with_version_zero_is_still_paper(monkeypatch):
    """The gate is never weakened: env=true alone (half-raised lock) must not
    tag records as live."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    assert live_gate.LIVE_VERSION == 0
    assert live_gate.trading_mode() == "paper"


def test_full_triple_lock_env_path_is_live(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr(live_gate, "LIVE_VERSION", 1)
    assert live_gate.trading_mode() == "live"


def test_version_bumped_but_paper_url_is_paper(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", PAPER_BASE_URL)
    monkeypatch.setattr(live_gate, "LIVE_VERSION", 1)
    assert live_gate.trading_mode() == "paper"


def test_broker_is_paper_true_wins(monkeypatch):
    # Even with a fully raised env lock, a paper broker object is authoritative.
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr(live_gate, "LIVE_VERSION", 1)
    assert live_gate.trading_mode(_BrokerStub(is_paper=True)) == "paper"


def test_broker_is_paper_false_is_live(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert live_gate.trading_mode(_BrokerStub(is_paper=False)) == "live"


def test_broker_missing_is_paper_attr_defaults_paper():
    assert live_gate.trading_mode(_BrokerStub(is_paper=None)) == "paper"


def test_broker_non_bool_is_paper_defaults_paper():
    assert live_gate.trading_mode(_BrokerStub(is_paper="live")) == "paper"


def test_alpaca_broker_exposes_is_paper_property():
    b = object.__new__(AlpacaBroker)  # skip __init__ (no alpaca-py needed)
    b._paper = True
    assert b.is_paper is True
    b._paper = False
    assert b.is_paper is False
    assert live_gate.trading_mode(b) == "live"


def test_alpaca_broker_refuses_non_paper_under_half_raised_lock(monkeypatch):
    """Codex P2 (PR #112): env=true alone (LIVE_VERSION still 0) must not be
    able to construct a live client — broker-less callers like the dashboard
    resync never pass through assert_live_gate, so the constructor is the
    gate that stops a half-raised lock from reaching the live account."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    assert live_gate.LIVE_VERSION == 0
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="triple"):
        AlpacaBroker(base_url="https://api.alpaca.markets")


def test_alpaca_broker_still_refuses_without_env_flag(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="triple"):
        AlpacaBroker(base_url="https://api.alpaca.markets")
