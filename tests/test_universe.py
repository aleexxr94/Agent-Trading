"""Invariants on the static universe (lib.universe)."""
from __future__ import annotations

import pytest

from lib import universe


def test_universe_non_empty():
    assert len(universe.UNIVERSE) >= 10  # we should never ship an empty universe


def test_universe_covers_multiple_uncorrelated_factors():
    """Earlier-cycle bug: every research candidate was a 3x broad-equity ETF,
    so the constructor abstained because nothing was uncorrelated. Universe
    must span at least 6 distinct factors (broad-eq long+short, sector,
    commodity, vol, crypto, rates) so the agent has real diversification
    options to choose from."""
    families = {e.family.split()[0] for e in universe.UNIVERSE}
    # Spot-check: at least 6 distinct family roots present
    assert len(families) >= 6, f"universe is too narrow — only {len(families)} factor roots: {families}"


def test_all_symbols_unique():
    syms = universe.all_symbols()
    assert len(syms) == len(set(syms)), "Duplicate symbols in universe"


def test_every_entry_has_required_fields():
    for e in universe.UNIVERSE:
        assert e.symbol and e.symbol.isupper(), f"Bad symbol: {e.symbol!r}"
        assert e.kind in ("etf", "option_underlying")
        assert e.family, f"{e.symbol}: empty family"
        assert e.description, f"{e.symbol}: empty description"


def test_option_underlyings_are_unleveraged_and_liquid():
    """The spec mandates the option-underlying sleeve consists of unleveraged
    liquid index ETFs. SPY/QQQ/IWM/DIA cover broad-equity factors; TLT adds
    rates/bonds (anti-correlated to equity-long, opens hedge plays)."""
    underlyings = [e for e in universe.UNIVERSE if e.kind == "option_underlying"]
    assert {e.symbol for e in underlyings} == {"SPY", "QQQ", "IWM", "DIA", "TLT"}
    for e in underlyings:
        assert e.leverage_factor == 1.0


def test_leveraged_etfs_have_leverage_at_least_1_5x():
    """Per CLAUDE.md the universe is 2x/3x leveraged ETFs — nothing weaker."""
    for e in universe.UNIVERSE:
        if e.kind == "etf":
            assert abs(e.leverage_factor) >= 1.5, (
                f"{e.symbol} has leverage {e.leverage_factor}, expected >= 1.5"
            )


def test_inverse_flag_matches_sign():
    for e in universe.UNIVERSE:
        assert e.is_inverse == (e.leverage_factor < 0)


def test_by_symbol_round_trip():
    e = universe.by_symbol("TQQQ")
    assert e is not None
    assert e.symbol == "TQQQ"
    assert e.leverage_factor == 3.0
    assert universe.by_symbol("NOT_A_REAL_TICKER") is None


def test_metadata_block_shape():
    block = universe.metadata_block()
    assert len(block) == len(universe.UNIVERSE)
    for row in block:
        assert set(row.keys()) == {"symbol", "kind", "leverage_factor", "family", "description"}


@pytest.mark.parametrize("expected", [
    "TQQQ", "SQQQ",  # Nasdaq
    "UPRO", "SPXU",  # S&P
    "TNA",  "TZA",   # Russell
    "SOXL", "SOXS",  # Semis
    "FAS",  "FAZ",   # Financials (broad)
    "DPST",          # Regional banks
    "LABU", "LABD",  # Biotech
    "CURE",          # Healthcare
    "YINN", "YANG",  # China
    "ERX",  "ERY",   # Energy
    "NUGT", "DUST",  # Gold miners
    "UVXY",          # Vol
    "BOIL",          # NatGas
    "BITX", "BITU", "SBIT", "ETHU",  # Crypto (leveraged-ETF exposure)
    "SPY", "QQQ", "IWM", "DIA", "TLT",  # Option underlyings
])
def test_canonical_symbols_present(expected):
    assert universe.by_symbol(expected) is not None, (
        f"{expected} dropped from universe — confirm intentional"
    )
