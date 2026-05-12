"""Invariants on the static universe (lib.universe)."""
from __future__ import annotations

import pytest

from lib import universe


def test_universe_non_empty():
    assert len(universe.UNIVERSE) >= 10  # we should never ship an empty universe


def test_universe_covers_multiple_uncorrelated_factors():
    """Earlier-cycle bug: every research candidate was a 3x broad-equity ETF,
    so the constructor abstained because nothing was uncorrelated. Universe
    must span at least 10 distinct factors (broad indices long+short, sector,
    commodity, vol, crypto, rates) so the agent has real diversification
    options to choose from.

    Uses the explicit `factor` field on UniverseEntry — bull/bear pairs of
    the same index share a factor, so `TQQQ` and `SQQQ` both count as
    `"nasdaq"` (a single factor), not two. Replaces the brittle earlier
    heuristic of splitting `family` on whitespace (Codex P2 on PR #30).
    """
    factors = {e.factor for e in universe.UNIVERSE}
    assert len(factors) >= 10, (
        f"universe spans only {len(factors)} factors: {sorted(factors)}. "
        "Diversification floor for this experiment is 10 — add more uncorrelated "
        "factor families before pruning below this threshold."
    )


def test_every_entry_has_non_empty_factor():
    """The `factor` field is mandatory — used by screener for diversification
    ranking and by the factor-coverage invariant above. Empty factor = silent
    classification gap."""
    for e in universe.UNIVERSE:
        assert e.factor and e.factor == e.factor.lower().strip(), (
            f"{e.symbol}: factor must be a non-empty lowercase identifier, got {e.factor!r}"
        )


def test_bull_bear_pairs_share_factor():
    """Bull/bear pairs of the same index must share a factor so the
    constructor's correlation check doesn't double-count them as
    'diversified across two factors'."""
    pairs = [
        ("TQQQ", "SQQQ"),
        ("UPRO", "SPXU"),
        ("TNA", "TZA"),
        ("URTY", "SRTY"),
        ("SOXL", "SOXS"),
        ("FAS", "FAZ"),
        ("LABU", "LABD"),
        ("YINN", "YANG"),
        ("ERX", "ERY"),
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
    """An option on SPY hits the same underlying factor as UPRO/SPXU — the
    constructor needs this to be visible so it can avoid loading the same
    factor twice (once via the leveraged ETF, once via its option underlying)."""
    pairs = [("SPY", "UPRO"), ("QQQ", "TQQQ"), ("IWM", "TNA")]
    for opt_sym, etf_sym in pairs:
        opt = universe.by_symbol(opt_sym)
        etf = universe.by_symbol(etf_sym)
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
        assert set(row.keys()) == {"symbol", "kind", "leverage_factor", "family", "factor", "description"}


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
