"""Tests for lib.alpaca_client — focused on the _normalize_asset_class helper
that absorbs the discrepancy between Alpaca's REST docs ('option') and the
alpaca-py SDK enum ('us_option').

We don't test the actual HTTP path (would require keys + network). The
helper is pure logic so a simple table-driven test is enough.
"""
from __future__ import annotations

from enum import Enum

import pytest

from lib.alpaca_client import _normalize_asset_class


class _FakeSDKEnum(str, Enum):
    """Stand-in for alpaca-py's AssetClass — same str-Enum mixin pattern."""
    US_EQUITY = "us_equity"
    US_OPTION = "us_option"
    CRYPTO = "crypto"


@pytest.mark.parametrize("raw,expected", [
    # alpaca-py SDK enum form
    (_FakeSDKEnum.US_OPTION, "us_option"),
    (_FakeSDKEnum.US_EQUITY, "us_equity"),
    # Raw REST string per Alpaca docs (lowercase 'option' / 'us_equity')
    ("option", "us_option"),
    ("us_option", "us_option"),
    ("us_equity", "us_equity"),
    # Defensive: uppercase / mixed case
    ("US_OPTION", "us_option"),
    ("OPTION", "us_option"),
    # Defensive: unexpected → default to equity (safer than option)
    ("crypto", "us_equity"),
    ("", "us_equity"),
    (None, "us_equity"),
])
def test_normalize_asset_class(raw, expected):
    assert _normalize_asset_class(raw) == expected


def test_str_enum_mixin_preserves_equality():
    """The alpaca-py AssetClass enum inherits from (str, Enum) so its members
    compare equal to their string value — the bug we were defending against
    is that the REST response can also surface a different *string* than the
    SDK enum exposes. Both normalise to 'us_option'."""
    assert _FakeSDKEnum.US_OPTION == "us_option"
    assert _normalize_asset_class(_FakeSDKEnum.US_OPTION) == _normalize_asset_class("option")


# ---------- get_clock UTC normalisation (Codex P2 on v2 PR) ----------


from datetime import datetime, timedelta, timezone

from lib.alpaca_client import AlpacaBroker
from lib.broker import MarketClock


class _FakeClock:
    """alpaca-py-like clock object — duck-typed for AlpacaBroker.get_clock."""
    def __init__(self, *, is_open, next_open, next_close, timestamp):
        self.is_open = is_open
        self.next_open = next_open
        self.next_close = next_close
        self.timestamp = timestamp


class _FakeTradingClient:
    def __init__(self, clock):
        self._clock = clock

    def get_clock(self):
        return self._clock


def _broker_with_clock(clock) -> AlpacaBroker:
    """Build an AlpacaBroker without going through the alpaca-py
    constructor (which needs real keys + network). We construct via
    object.__new__ so __init__ doesn't run, then attach the stub
    trading client.
    """
    b = object.__new__(AlpacaBroker)
    b._paper = True
    b._api_key = "stub"
    b._api_secret = "stub"
    b._client = _FakeTradingClient(clock)
    return b


def test_get_clock_returns_z_suffix_when_broker_returns_aware_utc():
    """Broker datetimes already aware-UTC: ISO output ends in Z."""
    broker = _broker_with_clock(_FakeClock(
        is_open=True,
        next_open=datetime(2026, 5, 14, 13, 30, 0, tzinfo=timezone.utc),
        next_close=datetime(2026, 5, 13, 20, 0, 0, tzinfo=timezone.utc),
        timestamp=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    ))
    clock = broker.get_clock()
    assert isinstance(clock, MarketClock)
    assert clock.is_open is True
    assert clock.next_open == "2026-05-14T13:30:00Z"
    assert clock.next_close == "2026-05-13T20:00:00Z"
    assert clock.timestamp == "2026-05-13T14:00:00Z"


def test_get_clock_normalises_non_utc_offset_to_z():
    """Codex P2: when alpaca-py returns datetimes with a non-UTC offset
    (e.g. ET during DST), get_clock MUST convert to UTC before
    formatting. An earlier version only rewrote `+00:00` → `Z` and
    returned ET-suffixed strings unchanged, which the downstream
    scheduler couldn't parse.

    9:30 AM ET (UTC-4 in DST) === 13:30 UTC.
    """
    et = timezone(timedelta(hours=-4))
    broker = _broker_with_clock(_FakeClock(
        is_open=False,
        next_open=datetime(2026, 5, 14, 9, 30, 0, tzinfo=et),
        next_close=datetime(2026, 5, 13, 16, 0, 0, tzinfo=et),
        timestamp=datetime(2026, 5, 13, 18, 0, 0, tzinfo=et),
    ))
    clock = broker.get_clock()
    assert clock is not None
    # 09:30 ET (UTC-4) → 13:30 UTC
    assert clock.next_open == "2026-05-14T13:30:00Z"
    # 16:00 ET → 20:00 UTC
    assert clock.next_close == "2026-05-13T20:00:00Z"
    # 18:00 ET → 22:00 UTC
    assert clock.timestamp == "2026-05-13T22:00:00Z"


def test_get_clock_handles_naive_datetime_as_utc():
    """If alpaca-py ever returned a naive datetime (no tzinfo), assume
    UTC. Defensive — alpaca-py returns aware datetimes in practice."""
    broker = _broker_with_clock(_FakeClock(
        is_open=True,
        next_open=None,
        next_close=datetime(2026, 5, 13, 20, 0, 0),  # naive
        timestamp=datetime(2026, 5, 13, 14, 0, 0),  # naive
    ))
    clock = broker.get_clock()
    assert clock.next_close == "2026-05-13T20:00:00Z"
    assert clock.timestamp == "2026-05-13T14:00:00Z"


def test_get_clock_returns_none_on_broker_exception():
    """A transient Alpaca API error must NOT crash the orchestrator —
    return None so market_gate falls open conservatively."""
    class _BoomClient:
        def get_clock(self):
            raise RuntimeError("network glitch")

    b = object.__new__(AlpacaBroker)
    b._paper = True
    b._client = _BoomClient()
    assert b.get_clock() is None
