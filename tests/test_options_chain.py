"""Tests for lib/options_chain — v2 stage between strategist and construct."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import options_chain, state


def test_nearest_otm_strike_call():
    """Call: smallest strike > spot."""
    assert options_chain._nearest_otm_strike([100, 105, 110], spot=102, side="call") == 105


def test_nearest_otm_strike_put():
    """Put: largest strike < spot."""
    assert options_chain._nearest_otm_strike([100, 105, 110], spot=108, side="put") == 105


def test_nearest_otm_strike_returns_none_when_no_otm_available():
    """All strikes are below spot → no OTM call possible."""
    assert options_chain._nearest_otm_strike([100, 105], spot=200, side="call") is None
    """All strikes above spot → no OTM put possible."""
    assert options_chain._nearest_otm_strike([100, 105], spot=50, side="put") is None


def test_nearest_otm_strike_handles_empty_or_zero_spot():
    assert options_chain._nearest_otm_strike([], spot=100, side="call") is None
    assert options_chain._nearest_otm_strike([100, 110], spot=0, side="call") is None


def test_target_expiry_window_returns_30_to_45_dte():
    lo, hi = options_chain._target_expiry_window(target_dte=37, tolerance_days=14)
    from datetime import date
    today = date.today()
    # 37 - 14 = 23, but clamped to >= 1.
    assert (lo - today).days >= 1
    assert (lo - today).days <= 23
    assert (hi - today).days == 51


def test_osi_symbol_format():
    """Standard OCC: 6-char date, C/P, 8-digit strike × 1000."""
    osi = options_chain._osi("SPY", "2026-06-19", "call", 540.0)
    assert osi == "SPY260619C00540000"
    osi_put = options_chain._osi("TLT", "2026-06-26", "put", 84.5)
    assert osi_put == "TLT260626P00084500"


def test_lookup_for_view_returns_empty_when_no_option_candidates():
    """View with only ETF candidates → no lookups produced."""
    view = {
        "run_id": "rid",
        "candidates": [
            {"symbol": "TQQQ", "instrument_kind": "etf",
             "thesis": "x", "confidence": 0.7}
        ],
    }
    signals_out = {"tickers": [{"symbol": "TQQQ", "last_close": 72.0}]}
    result = options_chain.lookup_for_view(view, signals_out, broker=None)
    assert result["lookups"] == []


def test_lookup_for_view_records_error_when_no_spot():
    """Option candidate but no spot price in signals → error row."""
    view = {
        "run_id": "rid",
        "candidates": [
            {"symbol": "SPY", "instrument_kind": "option_call",
             "thesis": "x", "confidence": 0.7}
        ],
    }
    signals_out = {"tickers": []}  # no SPY entry
    result = options_chain.lookup_for_view(view, signals_out, broker=None)
    assert len(result["lookups"]) == 1
    row = result["lookups"][0]
    assert row["contract"] is None
    assert "no spot price" in (row["error"] or "")


def test_lookup_for_view_with_no_broker_returns_no_contract():
    """No broker → lookup_nearest_otm returns None → contract field
    is None with an error message."""
    view = {
        "run_id": "rid",
        "candidates": [
            {"symbol": "SPY", "instrument_kind": "option_call",
             "thesis": "x", "confidence": 0.7}
        ],
    }
    signals_out = {"tickers": [{"symbol": "SPY", "last_close": 540.0}]}
    result = options_chain.lookup_for_view(view, signals_out, broker=None)
    assert result["lookups"][0]["contract"] is None
    assert result["lookups"][0]["error"]


def test_lookup_for_view_separates_call_and_put_candidates(tmp_state):
    """One call candidate + one put candidate → two lookup rows, both
    with the correct ``candidate.instrument_kind``."""
    view = {
        "run_id": "rid",
        "candidates": [
            {"symbol": "SPY", "instrument_kind": "option_call",
             "thesis": "x", "confidence": 0.7},
            {"symbol": "TLT", "instrument_kind": "option_put",
             "thesis": "y", "confidence": 0.6},
        ],
    }
    signals_out = {"tickers": [
        {"symbol": "SPY", "last_close": 540.0},
        {"symbol": "TLT", "last_close": 86.0},
    ]}
    result = options_chain.lookup_for_view(view, signals_out, broker=None)
    assert len(result["lookups"]) == 2
    kinds = [r["candidate"]["instrument_kind"] for r in result["lookups"]]
    assert sorted(kinds) == ["option_call", "option_put"]


def test_lookup_for_view_validates_against_schema(tmp_state):
    """Schema check: empty + populated outputs both validate."""
    view = {"run_id": "rid", "candidates": []}
    result = options_chain.lookup_for_view(view, {"tickers": []}, broker=None)
    state.validate(result, "chain_lookups.schema.json")


# ---------- Codex P1 on PR #69: chain pagination ----------


class _FakeContract:
    def __init__(self, expiration_date, strike_price):
        self.expiration_date = expiration_date
        self.strike_price = str(strike_price)


class _FakeOptionContractsResponse:
    def __init__(self, contracts, next_page_token=None):
        self.option_contracts = contracts
        self.next_page_token = next_page_token


class _FakePaginatingTradingClient:
    """Stub alpaca-py TradingClient that returns N pages of contracts."""
    def __init__(self, pages: list[list]):
        self._pages = pages
        self.requests_seen: list = []

    def get_option_contracts(self, req):
        self.requests_seen.append(req)
        # Pick the page whose index matches the request's page_token
        # (None for first page, then "1", "2", … for subsequent).
        token = getattr(req, "page_token", None)
        idx = 0 if token in (None, "") else int(token)
        if idx >= len(self._pages):
            return _FakeOptionContractsResponse([], next_page_token=None)
        page = self._pages[idx]
        next_token = str(idx + 1) if idx + 1 < len(self._pages) else None
        return _FakeOptionContractsResponse(page, next_page_token=next_token)


class _FakeBroker:
    """Minimal broker stub exposing _client for options_chain."""
    def __init__(self, client):
        self._client = client


def test_lookup_nearest_otm_follows_pagination_token():
    """Codex P1: SPY/QQQ chains in the 23-51 DTE window can exceed 500
    contracts. The original single-page fetch silently truncated the
    universe. Now lookup_nearest_otm follows next_page_token until
    exhausted (or hits the 50-page safety ceiling).
    """
    # Spread 3 pages: each page has 2 strikes at different distances
    # from spot. The TRUE nearest-OTM strike is on page 3 — proves
    # pagination is being followed.
    exp = "2026-06-19"
    page1 = [_FakeContract(exp, 600.0), _FakeContract(exp, 700.0)]
    page2 = [_FakeContract(exp, 580.0), _FakeContract(exp, 590.0)]
    page3 = [_FakeContract(exp, 541.0), _FakeContract(exp, 542.0)]  # 541 is nearest-OTM above 540
    client = _FakePaginatingTradingClient([page1, page2, page3])
    broker = _FakeBroker(client)

    contract = options_chain.lookup_nearest_otm(
        "SPY", side="call", spot=540.0, target_dte=37, broker=broker,
    )
    assert contract is not None, "pagination must surface contracts past page 1"
    # Without pagination we'd have only seen 580 (smallest >540 on page 1
    # was 600). With pagination we see 541.
    assert contract.strike == 541.0, (
        f"expected 541 (true nearest OTM across all pages), got {contract.strike}"
    )
    # All 3 pages were requested
    assert len(client.requests_seen) == 3


def test_lookup_nearest_otm_stops_when_next_page_token_is_none():
    """Single-page response: don't issue a second request."""
    exp = "2026-06-19"
    client = _FakePaginatingTradingClient([
        [_FakeContract(exp, 541.0), _FakeContract(exp, 600.0)],
    ])
    broker = _FakeBroker(client)
    contract = options_chain.lookup_nearest_otm(
        "SPY", side="call", spot=540.0, target_dte=37, broker=broker,
    )
    assert contract is not None
    assert contract.strike == 541.0
    assert len(client.requests_seen) == 1


def test_lookup_nearest_otm_uses_partial_pages_on_mid_pagination_error():
    """If page 2 fetch raises (transient API error) but page 1 returned
    contracts, fall back to ranking page-1 contracts rather than
    returning None and silently skipping the option trade."""
    class _MidErrorClient:
        def __init__(self):
            self.calls = 0
        def get_option_contracts(self, req):
            self.calls += 1
            if self.calls == 1:
                return _FakeOptionContractsResponse(
                    [_FakeContract("2026-06-19", 541.0)],
                    next_page_token="2",
                )
            raise RuntimeError("transient")

    broker = _FakeBroker(_MidErrorClient())
    contract = options_chain.lookup_nearest_otm(
        "SPY", side="call", spot=540.0, target_dte=37, broker=broker,
    )
    # Even with page 2 erroring, we got the page-1 contract back —
    # the previous version would have None'd out the whole lookup.
    assert contract is not None
    assert contract.strike == 541.0
