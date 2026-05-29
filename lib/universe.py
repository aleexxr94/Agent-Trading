"""Static universe of tradeable instruments — ETF-only (29 tickers).

The system trades **only** leveraged and inverse ETFs. Bullish theses are
expressed by holding a bull (positively-leveraged) ETF; bearish theses are
expressed by holding an inverse (negatively-leveraged) ETF. There are no
broker shorts and no options — "short Nasdaq" is long SQQQ, not a margin
short of TQQQ and not a put.

Migration note (2026-05-29): listed options were removed entirely. The
$2,500 paper account could never clear the 15%-per-position cap on a single
contract for most underlyings, and six months of paper trading produced
zero option positions. The seven option underlyings (SPY/QQQ/TLT/GLD/IWM/
XLF/XLE) and all option machinery (Greeks, IV, chains, premiums, OSI
symbols) were dropped. The factors those underlyings covered are now
expressed through real leveraged/inverse ETF pairs: rates via TMF/TMV,
energy via ERX/ERY, etc.

Universe composition (29):
  - 13 bull/bear leveraged-ETF pairs (26): Nasdaq, S&P 500, small-caps,
    semis, technology, biotech, China, financials, energy, oil & gas E&P,
    natural gas, rates, gold miners. Each pair covers one factor in both
    directions.
  - 2 solo entries: UVXY (long vol — no inverse counterpart), and the
    crypto-btc factor uses BITX (+2x bull) paired with BITI (-1x inverse).

`factor` is the short factor identifier, shared across bull/bear pairs
(e.g. TQQQ + SQQQ both → "nasdaq"). Used by:
  - the strategist prompt for diversification ranking
  - the constructor prompt to avoid double-loading the same factor
  - test_universe_covers_multiple_uncorrelated_factors as the metric
    for "is the universe still multi-factor?"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

InstrumentKind = Literal["etf"]


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    kind: InstrumentKind
    leverage_factor: float       # signed: +ve for bull ETFs, -ve for inverse
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
F_TECH         = "technology"
F_BIOTECH      = "biotech"
F_CHINA        = "china"
F_FIN_BROAD    = "financials-broad"
F_ENERGY       = "energy"
F_OIL_GAS_EP   = "oil-gas-ep"
F_NATGAS       = "natural-gas"
F_RATES        = "rates"
F_GOLD_MINERS  = "gold-miners"
F_VOL          = "vol"
F_CRYPTO_BTC   = "crypto-btc"


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
    # ---- Technology (broad tech sector) ----
    _e("TECL", "etf",  3.0, "Technology 3x long",
       "Direxion Daily Technology Bull 3x — 3x daily long Technology Select Sector", F_TECH),
    _e("TECS", "etf", -3.0, "Technology 3x short",
       "Direxion Daily Technology Bear 3x — 3x daily inverse Technology Select Sector", F_TECH),
    # ---- Biotech ----
    _e("LABU", "etf",  3.0, "Biotech 3x long",
       "Direxion Daily S&P Biotech Bull 3x — 3x daily long S&P Biotech Select", F_BIOTECH),
    _e("LABD", "etf", -3.0, "Biotech 3x short",
       "Direxion Daily S&P Biotech Bear 3x — 3x daily inverse S&P Biotech Select", F_BIOTECH),
    # ---- China ----
    _e("YINN", "etf",  3.0, "China 3x long",
       "Direxion Daily FTSE China Bull 3x — 3x daily long FTSE China 50", F_CHINA),
    _e("YANG", "etf", -3.0, "China 3x short",
       "Direxion Daily FTSE China Bear 3x — 3x daily inverse FTSE China 50", F_CHINA),
    # ---- Financials (broad) ----
    _e("FAS",  "etf",  3.0, "Financials 3x long",
       "Direxion Daily Financial Bull 3x — 3x daily long Russell 1000 Financials", F_FIN_BROAD),
    _e("FAZ",  "etf", -3.0, "Financials 3x short",
       "Direxion Daily Financial Bear 3x — 3x daily inverse Russell 1000 Financials", F_FIN_BROAD),
    # ---- Energy (broad sector) ----
    _e("ERX",  "etf",  2.0, "Energy 2x long",
       "Direxion Daily Energy Bull 2x — 2x daily long Energy Select Sector", F_ENERGY),
    _e("ERY",  "etf", -2.0, "Energy 2x short",
       "Direxion Daily Energy Bear 2x — 2x daily inverse Energy Select Sector", F_ENERGY),
    # ---- Oil & gas exploration & production ----
    _e("GUSH", "etf",  2.0, "Oil & Gas E&P 2x long",
       "Direxion Daily S&P Oil & Gas E&P Bull 2x — 2x daily long S&P Oil & Gas E&P", F_OIL_GAS_EP),
    _e("DRIP", "etf", -2.0, "Oil & Gas E&P 2x short",
       "Direxion Daily S&P Oil & Gas E&P Bear 2x — 2x daily inverse S&P Oil & Gas E&P", F_OIL_GAS_EP),
    # ---- Natural gas ----
    _e("BOIL", "etf",  2.0, "Natural Gas 2x long",
       "ProShares Ultra Bloomberg Natural Gas — 2x daily long natural gas futures", F_NATGAS),
    _e("KOLD", "etf", -2.0, "Natural Gas 2x short",
       "ProShares UltraShort Bloomberg Natural Gas — 2x daily inverse natural gas futures", F_NATGAS),
    # ---- Rates / long-duration Treasuries ----
    # Replaces the former TLT option underlying with a real leveraged pair.
    _e("TMF",  "etf",  3.0, "20+yr Treasury 3x long",
       "Direxion Daily 20+ Year Treasury Bull 3x — 3x daily long ICE 20+yr Treasury", F_RATES),
    _e("TMV",  "etf", -3.0, "20+yr Treasury 3x short",
       "Direxion Daily 20+ Year Treasury Bear 3x — 3x daily inverse ICE 20+yr Treasury", F_RATES),
    # ---- Gold miners ----
    # Gold tends to anti-correlate with risk-on regimes — NUGT rallies in
    # inflation shocks / dollar weakness. 2x (was 3x pre-2020 reverse split).
    _e("NUGT", "etf",  2.0, "Gold Miners 2x long",
       "Direxion Daily Gold Miners Bull 2x — 2x daily long NYSE Arca Gold Miners", F_GOLD_MINERS),
    _e("DUST", "etf", -2.0, "Gold Miners 2x short",
       "Direxion Daily Gold Miners Bear 2x — 2x daily inverse NYSE Arca Gold Miners", F_GOLD_MINERS),
    # ---- Vol / commodity (solo — no inverse counterpart) ----
    _e("UVXY", "etf",  1.5, "VIX 1.5x long",
       "ProShares Ultra VIX Short-Term Futures — 1.5x daily long VIX front-month", F_VOL),
    # ---- Crypto (bull BITX + inverse BITI) ----
    _e("BITX", "etf",  2.0, "Bitcoin 2x long",
       "Volatility Shares 2x Bitcoin Strategy ETF — 2x daily long BTC futures", F_CRYPTO_BTC),
    _e("BITI", "etf", -1.0, "Bitcoin 1x short",
       "ProShares Short Bitcoin Strategy ETF — 1x daily inverse BTC futures", F_CRYPTO_BTC),
)


UNIVERSE: tuple[UniverseEntry, ...] = _LEVERAGED_ETFS


def all_symbols() -> list[str]:
    return [e.symbol for e in UNIVERSE]


def by_symbol(symbol: str) -> UniverseEntry | None:
    for e in UNIVERSE:
        if e.symbol == symbol:
            return e
    return None


def factor_pair(symbol: str) -> tuple[str | None, str | None]:
    """Given a symbol, return (bull_symbol, bear_symbol) for its factor.

    Used by the strategist prompt to surface the "go bearish on factor X"
    expression (the inverse ETF) without making the LLM guess the right
    ticker. For factors with no inverse counterpart (vol), bear_symbol is
    None. When a factor has multiple bulls or bears, the highest-|leverage|
    entry wins so the result is deterministic.
    """
    entry = by_symbol(symbol)
    if entry is None:
        return None, None
    bull = bear = None
    bull_mag = bear_mag = -1.0
    for e in UNIVERSE:
        if e.factor != entry.factor:
            continue
        mag = abs(e.leverage_factor)
        if e.leverage_factor > 0 and mag > bull_mag:
            bull, bull_mag = e.symbol, mag
        elif e.leverage_factor < 0 and mag > bear_mag:
            bear, bear_mag = e.symbol, mag
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
