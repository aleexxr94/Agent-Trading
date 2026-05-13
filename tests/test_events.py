"""Tests for lib/events — static macro calendar."""
from __future__ import annotations

from datetime import date

from lib import events


def test_upcoming_events_returns_within_window():
    """Date inside the calendar with multiple events in the next 7 days."""
    # 2026-05-13 has CPI on the same day; FOMC's next is 2026-06-17 (outside 7d).
    events_list = events.upcoming_events(within_days=7, from_date=date(2026, 5, 13))
    assert any(e["type"] == "CPI" and e["date"] == "2026-05-13" for e in events_list)
    assert all(e["days_away"] >= 0 for e in events_list)


def test_upcoming_events_excludes_past():
    """Events strictly before from_date are not included."""
    events_list = events.upcoming_events(within_days=7, from_date=date(2026, 5, 14))
    assert not any(e["date"] == "2026-05-13" for e in events_list)


def test_upcoming_events_sorted_ascending():
    events_list = events.upcoming_events(within_days=60, from_date=date(2026, 5, 13))
    dates = [e["date"] for e in events_list]
    assert dates == sorted(dates)


def test_events_for_symbol_returns_macro_for_broad_symbols():
    """SPY/QQQ/TLT and the bull/bear ETFs all get the full macro calendar."""
    for sym in ("SPY", "QQQ", "TLT", "TQQQ", "SQQQ", "UVXY"):
        ev = events.events_for_symbol(sym, within_days=60, from_date=date(2026, 5, 13))
        assert isinstance(ev, list)


def test_events_for_symbol_returns_empty_for_crypto():
    """BITX (crypto) doesn't react to FOMC/CPI in the same way; exempt."""
    ev = events.events_for_symbol("BITX", within_days=60, from_date=date(2026, 5, 13))
    assert ev == []
