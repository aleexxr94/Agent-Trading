"""Static universe of tradeable instruments for the screener.

Per CLAUDE.md §System scope:
  - Leveraged ETFs (2x/3x equity, sector, vol, commodity, crypto-futures)
  - Listed options on liquid underlyings: the unleveraged index/bond ETFs
    we include here (SPY, QQQ, IWM, DIA, TLT), plus high-volume leveraged ETFs.
  - **No spot single-name equities. No unleveraged broad-market ETFs as
    core positions** — they're entered only via their listed options.

This list is curated, not exhaustive. Add/remove entries as the universe
evolves. The screener still applies liquidity filters strictly — anything
that fails ADV / spread checks at runtime is rejected even if listed here.

`factor` is the explicit factor-classification field. Bull/bear pairs of
the same index share the same `factor` (e.g. TQQQ + SQQQ both → "nasdaq",
UPRO + SPXU both → "sp500"). Used by:
  - the screener prompt for diversification ranking across candidates
  - test_universe_covers_multiple_uncorrelated_factors as the robust
    metric for "is the universe still single-factor?" (Codex P2 on PR #30
    flagged that splitting `family` on whitespace was a string-format
    artifact rather than a real classification).
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
# Adding a new factor here is a deliberate diversification expansion.
F_NASDAQ      = "nasdaq"
F_SP500       = "sp500"
F_SMALL_CAPS  = "small-caps"
F_SEMIS       = "semis"
F_FIN_BROAD   = "financials-broad"
F_FIN_REGNL   = "financials-regional"
F_BIOTECH     = "biotech"
F_HEALTHCARE  = "healthcare"
F_CHINA       = "china"
F_ENERGY      = "energy"
F_GOLD_MINERS = "gold-miners"
F_VOL         = "vol"
F_NATGAS      = "natgas"
F_CRYPTO_BTC  = "crypto-btc"
F_CRYPTO_ETH  = "crypto-eth"
F_DOW         = "dow"
F_RATES       = "rates"


def _e(symbol: str, kind: InstrumentKind, lev: float, family: str,
        description: str, factor: str) -> UniverseEntry:
    """Helper to keep entry construction readable below."""
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
    _e("URTY", "etf",  3.0, "Russell 2000 3x long (alt)",
       "ProShares UltraPro Russell2000 — 3x daily long Russell 2000", F_SMALL_CAPS),
    _e("SRTY", "etf", -3.0, "Russell 2000 3x short (alt)",
       "ProShares UltraPro Short Russell2000 — 3x daily inverse Russell 2000", F_SMALL_CAPS),
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
    # ---- Regional banks (different factor from broad financials) ----
    _e("DPST", "etf",  3.0, "Regional Banks 3x long",
       "Direxion Daily Regional Banks Bull 3x — 3x daily long S&P Regional Banks", F_FIN_REGNL),
    # ---- Biotech ----
    _e("LABU", "etf",  3.0, "Biotech 3x long",
       "Direxion Daily S&P Biotech Bull 3x — 3x daily long S&P Biotech", F_BIOTECH),
    _e("LABD", "etf", -3.0, "Biotech 3x short",
       "Direxion Daily S&P Biotech Bear 3x — 3x daily inverse S&P Biotech", F_BIOTECH),
    # ---- Healthcare ----
    _e("CURE", "etf",  3.0, "Healthcare 3x long",
       "Direxion Daily Healthcare Bull 3x — 3x daily long Russell 1000 Healthcare", F_HEALTHCARE),
    # ---- China ----
    _e("YINN", "etf",  3.0, "China 3x long",
       "Direxion Daily FTSE China Bull 3x — 3x daily long FTSE China 50", F_CHINA),
    _e("YANG", "etf", -3.0, "China 3x short",
       "Direxion Daily FTSE China Bear 3x — 3x daily inverse FTSE China 50", F_CHINA),
    # ---- Energy ----
    _e("ERX",  "etf",  2.0, "Energy 2x long",
       "Direxion Daily Energy Bull 2x — 2x daily long S&P Energy Select", F_ENERGY),
    _e("ERY",  "etf", -2.0, "Energy 2x short",
       "Direxion Daily Energy Bear 2x — 2x daily inverse S&P Energy Select", F_ENERGY),
    # ---- Gold miners ----
    _e("NUGT", "etf",  2.0, "Gold Miners 2x long",
       "Direxion Daily Gold Miners Bull 2x — 2x daily long NYSE Arca Gold Miners", F_GOLD_MINERS),
    _e("DUST", "etf", -2.0, "Gold Miners 2x short",
       "Direxion Daily Gold Miners Bear 2x — 2x daily inverse NYSE Arca Gold Miners", F_GOLD_MINERS),
    # ---- Volatility / commodity ----
    _e("UVXY", "etf",  1.5, "VIX 1.5x long",
       "ProShares Ultra VIX Short-Term Futures — 1.5x daily long VIX front-month", F_VOL),
    _e("BOIL", "etf",  2.0, "Natural Gas 2x long",
       "ProShares Ultra Bloomberg Natural Gas — 2x daily long NatGas futures", F_NATGAS),
    # ---- Crypto (leveraged-ETF exposure, in-spirit with spec) ----
    _e("BITX", "etf",  2.0, "Bitcoin 2x long (Volatility Shares)",
       "Volatility Shares 2x Bitcoin Strategy ETF — 2x daily long BTC futures", F_CRYPTO_BTC),
    _e("BITU", "etf",  2.0, "Bitcoin 2x long (ProShares)",
       "ProShares Ultra Bitcoin Strategy ETF — 2x daily long BTC futures", F_CRYPTO_BTC),
    _e("SBIT", "etf", -2.0, "Bitcoin 2x short",
       "ProShares UltraShort Bitcoin Strategy ETF — 2x daily inverse BTC futures", F_CRYPTO_BTC),
    _e("ETHU", "etf",  2.0, "Ether 2x long",
       "Volatility Shares 2x Ether ETF — 2x daily long ETH futures", F_CRYPTO_ETH),
)


_OPTION_UNDERLYINGS: tuple[UniverseEntry, ...] = (
    # SPY/QQQ/IWM share factors with their leveraged-ETF counterparts above —
    # an option on SPY hits the same underlying factor as UPRO/SPXU. Same for
    # QQQ↔TQQQ and IWM↔TNA. The constructor uses this to avoid double-loading.
    _e("SPY", "option_underlying", 1.0, "S&P 500 ETF",
       "Most liquid options chain in the world", F_SP500),
    _e("QQQ", "option_underlying", 1.0, "Nasdaq-100 ETF",
       "Tech-heavy options chain, very liquid", F_NASDAQ),
    _e("IWM", "option_underlying", 1.0, "Russell 2000 ETF",
       "Small-cap options chain", F_SMALL_CAPS),
    _e("DIA", "option_underlying", 1.0, "Dow Jones Industrial ETF",
       "Large-cap value tilt — different factor than SPY/QQQ", F_DOW),
    _e("TLT", "option_underlying", 1.0, "20+ Year Treasury Bond ETF",
       "Bond/rates exposure — anti-correlated to equity-long positions", F_RATES),
)


UNIVERSE: tuple[UniverseEntry, ...] = _LEVERAGED_ETFS + _OPTION_UNDERLYINGS


def all_symbols() -> list[str]:
    return [e.symbol for e in UNIVERSE]


def by_symbol(symbol: str) -> UniverseEntry | None:
    for e in UNIVERSE:
        if e.symbol == symbol:
            return e
    return None


def metadata_block() -> list[dict]:
    """Static metadata for every universe entry — kind, leverage, family,
    description, factor. Combined with live data from market_data.universe_snapshot()
    to form the full universe block sent to the screener.

    The `factor` field is what the screener uses to enforce cross-factor
    breadth on the `passed` list."""
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
