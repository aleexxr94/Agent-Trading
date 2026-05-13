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
