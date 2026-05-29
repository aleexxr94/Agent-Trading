"""Invariants on the static universe (lib.universe) — ETF-only (29 tickers).

The universe is leveraged/inverse ETFs only (options were removed). It has
13 bull/bear pairs + UVXY (solo vol) + BITX/BITI (crypto) = 29 tickers,
spanning 15 distinct factors. Bullish theses hold the bull ETF; bearish
theses hold the inverse ETF.
"""
from __future__ import annotations

import pytest

from lib import universe


def test_universe_size_is_29():
    """ETF-only universe has exactly 29 tickers: 13 bull/bear leveraged
    pairs (26) + UVXY (solo vol) + BITX/BITI (crypto). If you change this,
    update the factor-count floor below and the strategist prompt's
    universe section."""
    assert len(universe.UNIVERSE) == 29, (
        f"universe size {len(universe.UNIVERSE)} != 29 (ETF-only)."
    )


def test_universe_is_etf_only():
    """No option underlyings — every entry is a tradeable ETF."""
    for e in universe.UNIVERSE:
        assert e.kind == "etf", f"{e.symbol}: kind={e.kind!r}, expected 'etf'"


def test_no_option_underlyings_present():
    """The 7 former option underlyings are gone — they are not tradeable
    on an ETF-only system."""
    for sym in ("SPY", "QQQ", "TLT", "GLD", "IWM", "XLF", "XLE"):
        assert universe.by_symbol(sym) is None, (
            f"{sym} is an option underlying and must not be in the ETF-only universe"
        )


def test_universe_covers_multiple_uncorrelated_factors():
    """Universe must span ≥13 distinct factors. ETF expansion gave each
    sector its own bull/bear pair (tech, biotech, china, energy, oil-gas,
    nat-gas, rates) on top of the original equity/gold/vol/crypto set.
    Several equity factors are correlated risk-on beta — the constructor
    de-dupes by factor — but the factor *labels* stay distinct."""
    factors = {e.factor for e in universe.UNIVERSE}
    assert len(factors) >= 13, (
        f"universe spans only {len(factors)} factors: {sorted(factors)}."
    )


def test_every_entry_has_non_empty_factor():
    """The `factor` field is mandatory — used by the strategist for
    diversification ranking and by the constructor to avoid double-loading
    a factor."""
    for e in universe.UNIVERSE:
        assert e.factor and e.factor == e.factor.lower().strip(), (
            f"{e.symbol}: factor must be a non-empty lowercase identifier, got {e.factor!r}"
        )


def test_bull_bear_pairs_share_factor():
    """Every bull/inverse pair must share a factor so the constructor's
    correlation check doesn't double-count them."""
    pairs = [
        ("TQQQ", "SQQQ"),
        ("UPRO", "SPXU"),
        ("TNA",  "TZA"),
        ("SOXL", "SOXS"),
        ("TECL", "TECS"),
        ("LABU", "LABD"),
        ("YINN", "YANG"),
        ("FAS",  "FAZ"),
        ("ERX",  "ERY"),
        ("GUSH", "DRIP"),
        ("BOIL", "KOLD"),
        ("TMF",  "TMV"),
        ("NUGT", "DUST"),
    ]
    for bull_sym, bear_sym in pairs:
        bull = universe.by_symbol(bull_sym)
        bear = universe.by_symbol(bear_sym)
        assert bull is not None and bear is not None, f"{bull_sym}/{bear_sym} missing"
        assert bull.factor == bear.factor, (
            f"{bull_sym} factor={bull.factor!r} differs from {bear_sym} factor={bear.factor!r}"
        )
        assert bull.leverage_factor > 0 and bear.leverage_factor < 0, (
            f"{bull_sym} must be bull (+lev) and {bear_sym} inverse (-lev)"
        )


def test_all_symbols_unique():
    syms = universe.all_symbols()
    assert len(syms) == len(set(syms)), "Duplicate symbols in universe"


def test_every_entry_has_required_fields():
    for e in universe.UNIVERSE:
        assert e.symbol and e.symbol.isupper(), f"Bad symbol: {e.symbol!r}"
        assert e.kind == "etf"
        assert e.family, f"{e.symbol}: empty family"
        assert e.description, f"{e.symbol}: empty description"


def test_leveraged_etfs_have_leverage_at_least_1x():
    """Every entry is a leveraged/inverse ETF — BITI (1x inverse) is the
    magnitude floor; UVXY is 1.5x; most are 2x/3x."""
    for e in universe.UNIVERSE:
        assert abs(e.leverage_factor) >= 1.0, (
            f"{e.symbol} has leverage {e.leverage_factor}, expected magnitude >= 1.0"
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
        assert set(row.keys()) == {"symbol", "kind", "leverage_factor", "family", "factor", "description"}


def test_factor_pair_returns_bull_and_bear_for_paired_factors():
    """factor_pair("TQQQ") returns (TQQQ, SQQQ); factor_pair("UVXY")
    returns (UVXY, None) since vol has no inverse pair; crypto pairs the
    2x bull BITX with the 1x inverse BITI."""
    assert universe.factor_pair("TQQQ") == ("TQQQ", "SQQQ")
    assert universe.factor_pair("SOXS") == ("SOXL", "SOXS")
    assert universe.factor_pair("TMV") == ("TMF", "TMV")
    assert universe.factor_pair("BITX") == ("BITX", "BITI")
    bull, bear = universe.factor_pair("UVXY")
    assert bull == "UVXY"
    assert bear is None
    # Unknown symbol returns (None, None) — must not crash.
    assert universe.factor_pair("NOT_REAL") == (None, None)


@pytest.mark.parametrize("expected", [
    "TQQQ", "SQQQ",   # Nasdaq
    "UPRO", "SPXU",   # S&P
    "TNA",  "TZA",    # Russell small-caps
    "SOXL", "SOXS",   # Semis
    "TECL", "TECS",   # Technology
    "LABU", "LABD",   # Biotech
    "YINN", "YANG",   # China
    "FAS",  "FAZ",    # Financials
    "ERX",  "ERY",    # Energy
    "GUSH", "DRIP",   # Oil & gas E&P
    "BOIL", "KOLD",   # Natural gas
    "TMF",  "TMV",    # Rates
    "NUGT", "DUST",   # Gold miners
    "UVXY",           # Vol
    "BITX", "BITI",   # Crypto
])
def test_etf_universe_symbols_present(expected):
    assert universe.by_symbol(expected) is not None, (
        f"{expected} missing from the ETF-only universe — confirm intentional"
    )


@pytest.mark.parametrize("dropped", [
    "SPY", "QQQ", "TLT", "GLD", "IWM", "XLF", "XLE",  # former option underlyings
    "URTY", "SRTY",  # Russell alts (TNA/TZA cover the factor)
    "DPST",          # Regional banks
    "CURE",          # Healthcare
    "BITU", "SBIT", "ETHU",  # Crypto alts
    "DIA",           # former option underlying (correlates ~99% with SPY)
])
def test_dropped_symbols_absent(dropped):
    """Lock the trim: symbols intentionally excluded must NOT come back
    without a deliberate decision. Notably the 7 option underlyings were
    removed when options were dropped entirely."""
    assert universe.by_symbol(dropped) is None, (
        f"{dropped} is excluded but is back in the universe. "
        "If intentional, remove from this test's parametrize list."
    )
