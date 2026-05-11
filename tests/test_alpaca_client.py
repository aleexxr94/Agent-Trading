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
