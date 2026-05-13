"""Static universe of tradeable instruments — v2 (15 tickers).

Trimmed from the v1 33-instrument universe (PR ε predecessor) after the
LLM pipeline was simplified to a single deterministic-signals stage + one
LLM constructor call. Fewer candidates means sharper decisions; the v1
universe produced the same TLT-straddle outcome cycle after cycle, which
is the failure mode this trim is designed to fix.

Universe composition (15):
  - 5 bull/bear leveraged-ETF pairs (10): Nasdaq, S&P 500, semis,
    Russell 2000, financials. Each pair covers one factor in both
    directions — "short Nasdaq" is expressed as long SQQQ, not as a
    margin short of TQQQ. No actual broker shorts (synthetic only).
  - 2 solo leveraged ETFs: UVXY (vol), BITX (crypto bull). Different
    factor space from the equity bull/bear pairs.
  - 3 option underlyings: SPY, QQQ, TLT. SPY+QQQ for broad-market
    options (most liquid chains); TLT for rates exposure (no
    leveraged bond ETF in this universe, so options are the entry).

Dropped from v1:
  - Russell 2000 alts (URTY/SRTY) — TNA/TZA already cover the factor
  - Regional banks (DPST) — no bear pair; redundant with FAS/FAZ broad
  - Biotech (LABU/LABD), healthcare (CURE), China (YINN/YANG), energy
    (ERX/ERY), gold miners (NUGT/DUST), nat gas (BOIL), Ether (ETHU),
    bitcoin alts (BITU/SBIT) — lower ADV; factor-redundant or speculative
  - IWM, DIA as option underlyings — Russell 2000 is covered via
    TNA/TZA already; DIA correlates ~99% with SPY.

`factor` is the short factor identifier, shared across bull/bear pairs
(e.g. TQQQ + SQQQ both → "nasdaq"). Used by:
  - the strategist prompt for diversification ranking
  - the constructor prompt to avoid double-loading (e.g. don't pick
    both QQQ call and TQQQ long — same factor)
  - test_universe_covers_multiple_uncorrelated_factors as the metric
    for "is the universe still single-factor?"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

InstrumentKind = Literal["etf", "option_underlying"]


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    kind: InstrumentKind
    leverage_factor: float       # 1.0 for option underlyings; 2/3 for leveraged ETFs
    family: str                   # human label e.g. "Nasdaq 3x long"
    description: str
    factor: str                   # short factor identifier, shared across bull/bear pairs

    @property
    def is_inverse(self) -> bool:
        return self.leverage_factor < 0


# Factor classification — short identifiers shared across bull/bear pairs.
F_NASDAQ       = "nasdaq"
F_SP500        = "sp500"
F_SMALL_CAPS   = "small-caps"
F_SEMIS        = "semis"
F_FIN_BROAD    = "financials-broad"
F_VOL          = "vol"
F_CRYPTO_BTC   = "crypto-btc"
F_RATES        = "rates"
# Gold added on 2026-05-13 (post-v2 universe expansion). Gold miners
# (NUGT/DUST) and spot gold (GLD) share gold beta but are NOT identical:
# miners carry operational leverage + equity beta on top of the gold
# move, while spot gold (GLD) is the pure metal tracker. Distinct
# factors so the strategist can pair them without the per-underlying
# concentration sanity rule treating them as one position. The
# constructor's correlation check should still bias against loading
# both heavily — they ARE correlated, just not identical.
F_GOLD_MINERS  = "gold-miners"
F_GOLD_SPOT    = "gold-spot"


def _e(symbol: str, kind: InstrumentKind, lev: float, family: str,
        description: str, factor: str) -> UniverseEntry:
    return UniverseEntry(symbol, kind, lev, family, description, factor)


_LEVERAGED_ETFS: tuple[UniverseEntry, ...] = (
    # ---- Nasdaq-100 ----
    _e("TQQQ", "etf",  3.0, "Nasdaq 3x long",
       "ProShares UltraPro QQQ — 3x daily long Nasdaq-100", F_NASDAQ),
    _e("SQQQ", "etf", -3.0, "Nasdaq 3x short",
       "ProShares UltraPro Short QQQ — 3x daily inverse Nasdaq-100", F_NASDAQ),
    # ---- S&P 500 ----
    _e("UPRO", "etf",  3.0, "S&P 500 3x long",
       "ProShares UltraPro S&P500 — 3x daily long S&P 500", F_SP500),
    _e("SPXU", "etf", -3.0, "S&P 500 3x short",
       "ProShares UltraPro Short S&P500 — 3x daily inverse S&P 500", F_SP500),
    # ---- Russell 2000 / small caps ----
    _e("TNA",  "etf",  3.0, "Russell 2000 3x long",
       "Direxion Daily Small Cap Bull 3x — 3x daily long Russell 2000", F_SMALL_CAPS),
    _e("TZA",  "etf", -3.0, "Russell 2000 3x short",
       "Direxion Daily Small Cap Bear 3x — 3x daily inverse Russell 2000", F_SMALL_CAPS),
    # ---- Semiconductors ----
    _e("SOXL", "etf",  3.0, "Semis 3x long",
       "Direxion Daily Semiconductor Bull 3x — 3x daily long PHLX Semi", F_SEMIS),
    _e("SOXS", "etf", -3.0, "Semis 3x short",
       "Direxion Daily Semiconductor Bear 3x — 3x daily inverse PHLX Semi", F_SEMIS),
    # ---- Financials (broad) ----
    _e("FAS",  "etf",  3.0, "Financials 3x long",
       "Direxion Daily Financial Bull 3x — 3x daily long Russell 1000 Financials", F_FIN_BROAD),
    _e("FAZ",  "etf", -3.0, "Financials 3x short",
       "Direxion Daily Financial Bear 3x — 3x daily inverse Russell 1000 Financials", F_FIN_BROAD),
    # ---- Vol / commodity ----
    _e("UVXY", "etf",  1.5, "VIX 1.5x long",
       "ProShares Ultra VIX Short-Term Futures — 1.5x daily long VIX front-month", F_VOL),
    # ---- Crypto ----
    _e("BITX", "etf",  2.0, "Bitcoin 2x long",
       "Volatility Shares 2x Bitcoin Strategy ETF — 2x daily long BTC futures", F_CRYPTO_BTC),
    # ---- Gold miners (added 2026-05-13) ----
    # Factor-diversifier vs the equity bull/bear pairs above. Gold tends
    # to anti-correlate with risk-on regimes — NUGT rallies in inflation
    # shocks / dollar weakness. 2x (was 3x pre-2020 reverse split).
    _e("NUGT", "etf",  2.0, "Gold Miners 2x long",
       "Direxion Daily Gold Miners Bull 2x — 2x daily long NYSE Arca Gold Miners", F_GOLD_MINERS),
    _e("DUST", "etf", -2.0, "Gold Miners 2x short",
       "Direxion Daily Gold Miners Bear 2x — 2x daily inverse NYSE Arca Gold Miners", F_GOLD_MINERS),
)


_OPTION_UNDERLYINGS: tuple[UniverseEntry, ...] = (
    # SPY shares factor with UPRO/SPXU; QQQ with TQQQ/SQQQ. The constructor
    # uses `factor` to avoid double-loading the same factor across an ETF
    # and an option leg.
    _e("SPY", "option_underlying", 1.0, "S&P 500 ETF",
       "Most liquid options chain in the world", F_SP500),
    _e("QQQ", "option_underlying", 1.0, "Nasdaq-100 ETF",
       "Tech-heavy options chain, very liquid", F_NASDAQ),
    _e("TLT", "option_underlying", 1.0, "20+ Year Treasury Bond ETF",
       "Bond/rates exposure — anti-correlated to equity-long positions", F_RATES),
    # GLD added 2026-05-13 alongside NUGT/DUST. Distinct factor
    # (gold-spot vs gold-miners) because spot gold has no operational
    # leverage or equity beta — it's the cleanest gold expression for
    # long-call/long-put plays around inflation prints, FOMC, currency
    # moves. Options chain is extremely liquid (~$1B+ ADV on the ETF).
    _e("GLD", "option_underlying", 1.0, "SPDR Gold Shares",
       "Pure spot-gold tracker — long calls/puts express directional "
       "gold view without the equity-beta + operational-leverage overlay "
       "that NUGT/DUST carry", F_GOLD_SPOT),
)


UNIVERSE: tuple[UniverseEntry, ...] = _LEVERAGED_ETFS + _OPTION_UNDERLYINGS


def all_symbols() -> list[str]:
    return [e.symbol for e in UNIVERSE]


def by_symbol(symbol: str) -> UniverseEntry | None:
    for e in UNIVERSE:
        if e.symbol == symbol:
            return e
    return None


def factor_pair(symbol: str) -> tuple[str | None, str | None]:
    """Given a symbol, return (bull_symbol, bear_symbol) for its factor.

    Used by the strategist prompt to surface the "go short on factor X"
    option without making the LLM guess the right ticker. For factors
    with no bear pair (vol, crypto-btc, rates), bear_symbol is None.
    """
    entry = by_symbol(symbol)
    if entry is None:
        return None, None
    bull = bear = None
    for e in UNIVERSE:
        if e.factor == entry.factor and e.kind == "etf":
            if e.leverage_factor > 0:
                bull = e.symbol
            elif e.leverage_factor < 0:
                bear = e.symbol
    return bull, bear


def metadata_block() -> list[dict]:
    """Static metadata for every universe entry — used by lib.signals to
    decorate the deterministic feature rows it computes per ticker."""
    return [
        {
            "symbol": e.symbol,
            "kind": e.kind,
            "leverage_factor": e.leverage_factor,
            "family": e.family,
            "factor": e.factor,
            "description": e.description,
        }
        for e in UNIVERSE
    ]
