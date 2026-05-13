"""Invariants on the static universe (lib.universe) — v2.

v2 universe is 15 tickers (was 33 in v1). The diversification floor
dropped from 10 factors to 8 — bull/bear pairs share a factor so 5
pairs = 5 factors, plus vol + crypto-btc + rates from solo entries
and SPY/QQQ option underlyings reusing sp500/nasdaq factors = 8.
"""
from __future__ import annotations

import pytest

from lib import universe


def test_universe_size_is_18():
    """v2 universe (post gold expansion 2026-05-13) has exactly 18
    tickers: 12 leveraged ETFs (5 equity bull/bear pairs + UVXY + BITX
    + NUGT/DUST gold-miners pair) + 4 option underlyings (SPY/QQQ/TLT
    + GLD). If you're adding to this list, update the factor-count
    floor below; if you're shrinking it, double-check the strategist
    prompt's universe section matches.
    """
    assert len(universe.UNIVERSE) == 18, (
        f"universe size {len(universe.UNIVERSE)} != 18. v2 + gold expansion."
    )


def test_universe_covers_multiple_uncorrelated_factors():
    """v2+gold must span ≥10 distinct factors: nasdaq, sp500, semis,
    small-caps, financials-broad (5 bull/bear equity pairs) +
    gold-miners (NUGT/DUST) + vol + crypto-btc + rates (option) +
    gold-spot (GLD option). Bull/bear pairs share a factor."""
    factors = {e.factor for e in universe.UNIVERSE}
    assert len(factors) >= 10, (
        f"universe spans only {len(factors)} factors: {sorted(factors)}. "
        "v2+gold floor is 10 — adding factors is encouraged, but pruning "
        "below this floor weakens diversification options."
    )


def test_every_entry_has_non_empty_factor():
    """The `factor` field is mandatory — used by strategist for
    diversification ranking and by the constructor to avoid
    double-loading an ETF + its option underlying counterpart."""
    for e in universe.UNIVERSE:
        assert e.factor and e.factor == e.factor.lower().strip(), (
            f"{e.symbol}: factor must be a non-empty lowercase identifier, got {e.factor!r}"
        )


def test_bull_bear_pairs_share_factor():
    """v2+gold bull/bear pairs (6 of them) must share a factor so the
    constructor's correlation check doesn't double-count them."""
    pairs = [
        ("TQQQ", "SQQQ"),
        ("UPRO", "SPXU"),
        ("TNA",  "TZA"),
        ("SOXL", "SOXS"),
        ("FAS",  "FAZ"),
        ("NUGT", "DUST"),
    ]
    for bull_sym, bear_sym in pairs:
        bull = universe.by_symbol(bull_sym)
        bear = universe.by_symbol(bear_sym)
        assert bull is not None and bear is not None, f"{bull_sym}/{bear_sym} missing"
        assert bull.factor == bear.factor, (
            f"{bull_sym} factor={bull.factor!r} differs from {bear_sym} factor={bear.factor!r} "
            "— same-index bull/bear pairs must share a factor"
        )


def test_option_underlyings_share_factor_with_their_leveraged_counterparts():
    """An option on SPY hits the same underlying factor as UPRO/SPXU —
    the constructor uses this to avoid loading the same factor twice."""
    pairs = [("SPY", "UPRO"), ("QQQ", "TQQQ")]
    for opt_sym, etf_sym in pairs:
        opt = universe.by_symbol(opt_sym)
        etf = universe.by_symbol(etf_sym)
        assert opt is not None and etf is not None
        assert opt.factor == etf.factor, (
            f"{opt_sym} factor={opt.factor!r} differs from {etf_sym} factor={etf.factor!r}"
        )


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
    """v2+gold option underlyings: SPY + QQQ for broad-equity options,
    TLT for rates exposure, GLD for spot-gold (added 2026-05-13
    alongside NUGT/DUST leveraged miners).
    """
    underlyings = [e for e in universe.UNIVERSE if e.kind == "option_underlying"]
    assert {e.symbol for e in underlyings} == {"SPY", "QQQ", "TLT", "GLD"}
    for e in underlyings:
        assert e.leverage_factor == 1.0


def test_leveraged_etfs_have_leverage_at_least_1_5x():
    """Per CLAUDE.md the universe is 2x/3x leveraged ETFs — UVXY at 1.5x
    is the floor."""
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
        assert set(row.keys()) == {"symbol", "kind", "leverage_factor", "family", "factor", "description"}


def test_factor_pair_returns_bull_and_bear_for_paired_factors():
    """factor_pair("TQQQ") returns (TQQQ, SQQQ); factor_pair("UVXY")
    returns (UVXY, None) since vol has no bear pair in v2."""
    assert universe.factor_pair("TQQQ") == ("TQQQ", "SQQQ")
    assert universe.factor_pair("SOXS") == ("SOXL", "SOXS")
    bull, bear = universe.factor_pair("UVXY")
    assert bull == "UVXY"
    assert bear is None
    # Unknown symbol returns (None, None) — strategist won't see this case
    # but the helper must not crash.
    assert universe.factor_pair("NOT_REAL") == (None, None)


@pytest.mark.parametrize("expected", [
    "TQQQ", "SQQQ",  # Nasdaq
    "UPRO", "SPXU",  # S&P
    "TNA",  "TZA",   # Russell
    "SOXL", "SOXS",  # Semis
    "FAS",  "FAZ",   # Financials (broad)
    "UVXY",          # Vol
    "BITX",          # Crypto
    "NUGT", "DUST",  # Gold miners (added 2026-05-13)
    "SPY", "QQQ", "TLT", "GLD",  # Option underlyings (GLD added 2026-05-13)
])
def test_v2_universe_symbols_present(expected):
    assert universe.by_symbol(expected) is not None, (
        f"{expected} missing from v2 universe — confirm intentional"
    )


@pytest.mark.parametrize("dropped", [
    "URTY", "SRTY",  # Russell alts (TNA/TZA cover the factor)
    "DPST",          # Regional banks
    "LABU", "LABD",  # Biotech
    "CURE",          # Healthcare
    "YINN", "YANG",  # China
    "ERX",  "ERY",   # Energy
    "BOIL",          # NatGas
    "BITU", "SBIT", "ETHU",  # Crypto alts
    "IWM", "DIA",    # Option underlyings dropped (factor-covered or correlated)
    # NUGT/DUST were on this list in v2 base; added back in the
    # gold-expansion 2026-05-13. If they reappear here, gold's been
    # removed again — make that an explicit decision.
])
def test_v1_dropped_symbols_absent(dropped):
    """Lock the v2 trim: symbols intentionally cut from the v1 universe
    must NOT come back without a deliberate decision. If a future PR
    adds one of these back, this test fails and forces an explicit
    update to the dropped-set list."""
    assert universe.by_symbol(dropped) is None, (
        f"{dropped} was dropped in v2 but is back in the universe. "
        "If intentional, remove from this test's parametrize list."
    )
