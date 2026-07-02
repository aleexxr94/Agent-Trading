"""Static universe of tradeable instruments — ETF-only (71 tickers).

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

Expansion note (2026-06-10, user-authorized): widened from 29 to 49
tickers so the agent has more genuinely independent factors to pick
trades from — commodities (crude, gold bullion, silver), geographies
(emerging markets), style (Dow, high-beta, internet), defensives-ish
sectors (healthcare, homebuilders, defense, regional banks) and a second
crypto factor (ether). Every addition is a listed leveraged/inverse ETF
whose dollar ADV comfortably clears the ≤1%-of-ADV sanity rule at this
account's position sizes (~$200-600 notional).

Single-stock note (2026-06-10, user-authorized): liquid LEVERAGED
single-stock ETFs were added — NVDL/NVD (NVDA), TSLL/TSLZ (TSLA),
MSTU/MSTZ (MSTR), CONL (COIN, solo bull). The CLAUDE.md ban on *spot*
single-name equities stands; these are listed leveraged ETFs and ride
the same caps/kill rails as every other position. They carry
single-company event risk (earnings, guidance) that the macro calendar
does not cover — the prompts call this out.

Expansion note (2026-07-02, user-authorized): widened from 57 to 71
tickers. Six solo sector/geography bulls (UTSL utilities, RETL retail,
BRZU Brazil, INDL India, EURL Europe, KORU South Korea) add defensive,
consumer and single-country factors with genuinely distinct macro
drivers; four Direxion single-stock lines (PLTU/PLTD on PLTR, AMZU/AMZD
on AMZN, GGLL/GGLS on GOOGL, METU/METD on META) follow the asymmetric
+2x bull / -1x bear pattern BITX/BITI already established. Cost impact
is negligible (~+500-600 tokens in the compacted signals block per
cycle); every addition clears the ≤1%-of-ADV sanity rule with wide
margin at this account's position sizes.

Universe composition (71):
  - 25 bull/bear leveraged-ETF pairs (50): Nasdaq, S&P 500, Dow,
    small-caps, high-beta, semis, technology, internet, biotech, China,
    emerging markets, financials, energy, oil & gas E&P, natural gas,
    crude oil, rates, gold miners, gold bullion, silver, ether, vol
    (UVXY/SVIX), NVDA, TSLA, MSTR. Each pair covers one factor in both
    directions.
  - 10 asymmetric +2x bull / -1x bear single-stock/crypto lines: BITX/
    BITI (crypto-btc), PLTU/PLTD (pltr), AMZU/AMZD (amzn), GGLL/GGLS
    (googl), METU/METD (meta).
  - 11 solo bull ETFs with no liquid inverse counterpart (NAIL
    homebuilders, DFEN defense, CURE healthcare, DPST regional banks,
    CONL coin, UTSL utilities, RETL retail, BRZU Brazil, INDL India,
    EURL Europe, KORU South Korea) — bearish views on those factors
    are expressed by not holding them (or via a correlated inverse,
    the agent's call).

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
# 2026-06-10 expansion factors.
F_DOW          = "dow"
F_EMERGING     = "emerging-markets"
F_INTERNET     = "internet"
F_HIGH_BETA    = "high-beta"
F_CRUDE_OIL    = "crude-oil"
F_SILVER       = "silver"
F_GOLD_BULLION = "gold-bullion"
F_CRYPTO_ETH   = "crypto-eth"
F_HOMEBUILDERS = "homebuilders"
F_DEFENSE      = "defense"
F_HEALTHCARE   = "healthcare"
F_REGIONAL_BANKS = "regional-banks"
# 2026-06-10 single-stock leveraged ETFs (user-authorized; the CLAUDE.md ban
# on SPOT single-name equities stands — these are listed leveraged ETFs).
F_NVDA         = "nvda"
F_TSLA         = "tsla"
F_MSTR         = "mstr"
F_COIN         = "coin"
# 2026-07-02 expansion factors (user-authorized).
F_UTILITIES    = "utilities"
F_RETAIL       = "retail"
F_BRAZIL       = "brazil"
F_INDIA        = "india"
F_EUROPE       = "europe"
F_KOREA        = "korea"
F_PLTR         = "pltr"
F_AMZN         = "amzn"
F_GOOGL        = "googl"
F_META         = "meta"


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
    # ---- Vol (UVXY long + SVIX short — paired as of 2026-06-10) ----
    _e("UVXY", "etf",  1.5, "VIX 1.5x long",
       "ProShares Ultra VIX Short-Term Futures — 1.5x daily long VIX front-month", F_VOL),
    _e("SVIX", "etf", -1.0, "VIX 1x short",
       "Volatility Shares -1x Short VIX Futures — 1x daily inverse VIX front-month", F_VOL),
    # ---- Crypto (bull BITX + inverse BITI) ----
    _e("BITX", "etf",  2.0, "Bitcoin 2x long",
       "Volatility Shares 2x Bitcoin Strategy ETF — 2x daily long BTC futures", F_CRYPTO_BTC),
    _e("BITI", "etf", -1.0, "Bitcoin 1x short",
       "ProShares Short Bitcoin Strategy ETF — 1x daily inverse BTC futures", F_CRYPTO_BTC),

    # ==== 2026-06-10 expansion (user-authorized): 8 new pairs + 4 solos ====
    # ---- Dow Jones Industrial Average ----
    _e("UDOW", "etf",  3.0, "Dow 3x long",
       "ProShares UltraPro Dow30 — 3x daily long Dow Jones Industrial Average", F_DOW),
    _e("SDOW", "etf", -3.0, "Dow 3x short",
       "ProShares UltraPro Short Dow30 — 3x daily inverse Dow Jones Industrial Average", F_DOW),
    # ---- Emerging markets ----
    _e("EDC",  "etf",  3.0, "Emerging Markets 3x long",
       "Direxion Daily MSCI Emerging Markets Bull 3x — 3x daily long MSCI EM", F_EMERGING),
    _e("EDZ",  "etf", -3.0, "Emerging Markets 3x short",
       "Direxion Daily MSCI Emerging Markets Bear 3x — 3x daily inverse MSCI EM", F_EMERGING),
    # ---- Internet ----
    _e("WEBL", "etf",  3.0, "Internet 3x long",
       "Direxion Daily Dow Jones Internet Bull 3x — 3x daily long DJ Internet Composite", F_INTERNET),
    _e("WEBS", "etf", -3.0, "Internet 3x short",
       "Direxion Daily Dow Jones Internet Bear 3x — 3x daily inverse DJ Internet Composite", F_INTERNET),
    # ---- S&P 500 high beta ----
    _e("HIBL", "etf",  3.0, "High Beta 3x long",
       "Direxion Daily S&P 500 High Beta Bull 3x — 3x daily long S&P 500 High Beta", F_HIGH_BETA),
    _e("HIBS", "etf", -3.0, "High Beta 3x short",
       "Direxion Daily S&P 500 High Beta Bear 3x — 3x daily inverse S&P 500 High Beta", F_HIGH_BETA),
    # ---- Crude oil (futures-based; distinct from equity energy ERX/ERY) ----
    _e("UCO",  "etf",  2.0, "Crude Oil 2x long",
       "ProShares Ultra Bloomberg Crude Oil — 2x daily long WTI crude futures", F_CRUDE_OIL),
    _e("SCO",  "etf", -2.0, "Crude Oil 2x short",
       "ProShares UltraShort Bloomberg Crude Oil — 2x daily inverse WTI crude futures", F_CRUDE_OIL),
    # ---- Silver ----
    _e("AGQ",  "etf",  2.0, "Silver 2x long",
       "ProShares Ultra Silver — 2x daily long silver bullion", F_SILVER),
    _e("ZSL",  "etf", -2.0, "Silver 2x short",
       "ProShares UltraShort Silver — 2x daily inverse silver bullion", F_SILVER),
    # ---- Gold bullion (distinct from gold MINERS NUGT/DUST) ----
    _e("UGL",  "etf",  2.0, "Gold 2x long",
       "ProShares Ultra Gold — 2x daily long gold bullion", F_GOLD_BULLION),
    _e("GLL",  "etf", -2.0, "Gold 2x short",
       "ProShares UltraShort Gold — 2x daily inverse gold bullion", F_GOLD_BULLION),
    # ---- Ether (second crypto factor; BTC and ETH regularly decorrelate) ----
    _e("ETHU", "etf",  2.0, "Ether 2x long",
       "Volatility Shares 2x Ether ETF — 2x daily long ETH futures", F_CRYPTO_ETH),
    _e("ETHD", "etf", -2.0, "Ether 2x short",
       "ProShares UltraShort Ether ETF — 2x daily inverse ETH futures", F_CRYPTO_ETH),
    # ---- Solo bull 3x sector ETFs (no liquid inverse counterpart) ----
    _e("NAIL", "etf",  3.0, "Homebuilders 3x long",
       "Direxion Daily Homebuilders & Supplies Bull 3x — 3x daily long DJ US Select Home Construction", F_HOMEBUILDERS),
    _e("DFEN", "etf",  3.0, "Aerospace & Defense 3x long",
       "Direxion Daily Aerospace & Defense Bull 3x — 3x daily long DJ US Select Aerospace & Defense", F_DEFENSE),
    _e("CURE", "etf",  3.0, "Healthcare 3x long",
       "Direxion Daily Healthcare Bull 3x — 3x daily long Health Care Select Sector", F_HEALTHCARE),
    _e("DPST", "etf",  3.0, "Regional Banks 3x long",
       "Direxion Daily Regional Banks Bull 3x — 3x daily long S&P Regional Banks Select", F_REGIONAL_BANKS),

    # ==== 2026-06-10 single-stock leveraged ETFs (user-authorized) ====
    # Single-COMPANY risk: idiosyncratic event exposure (earnings, guidance,
    # litigation) that the macro calendar does not cover. The per-position
    # entry cap / hold ceiling / kill conditions bound the blast radius.
    _e("NVDL", "etf",  2.0, "NVDA 2x long",
       "GraniteShares 2x Long NVDA Daily ETF — 2x daily long NVIDIA", F_NVDA),
    _e("NVD",  "etf", -2.0, "NVDA 2x short",
       "GraniteShares 2x Short NVDA Daily ETF — 2x daily inverse NVIDIA", F_NVDA),
    _e("TSLL", "etf",  2.0, "TSLA 2x long",
       "Direxion Daily TSLA Bull 2x — 2x daily long Tesla", F_TSLA),
    _e("TSLZ", "etf", -2.0, "TSLA 2x short",
       "T-Rex 2x Inverse Tesla Daily Target ETF — 2x daily inverse Tesla", F_TSLA),
    _e("MSTU", "etf",  2.0, "MSTR 2x long",
       "T-Rex 2x Long MSTR Daily Target ETF — 2x daily long MicroStrategy", F_MSTR),
    _e("MSTZ", "etf", -2.0, "MSTR 2x short",
       "T-Rex 2x Inverse MSTR Daily Target ETF — 2x daily inverse MicroStrategy", F_MSTR),
    _e("CONL", "etf",  2.0, "COIN 2x long",
       "GraniteShares 2x Long COIN Daily ETF — 2x daily long Coinbase (solo; no liquid inverse)", F_COIN),

    # ==== 2026-07-02 expansion (user-authorized): 6 solo bulls + 4 pairs ====
    # ---- Solo bull sector/geography ETFs (no liquid inverse counterpart) ----
    _e("UTSL", "etf",  3.0, "Utilities 3x long",
       "Direxion Daily Utilities Bull 3x — 3x daily long Utilities Select Sector", F_UTILITIES),
    _e("RETL", "etf",  3.0, "Retail 3x long",
       "Direxion Daily Retail Bull 3x — 3x daily long S&P Retail Select", F_RETAIL),
    _e("BRZU", "etf",  2.0, "Brazil 2x long",
       "Direxion Daily MSCI Brazil Bull 2x — 2x daily long MSCI Brazil 25/50", F_BRAZIL),
    _e("INDL", "etf",  2.0, "India 2x long",
       "Direxion Daily MSCI India Bull 2x — 2x daily long MSCI India", F_INDIA),
    _e("EURL", "etf",  3.0, "Europe 3x long",
       "Direxion Daily FTSE Europe Bull 3x — 3x daily long FTSE Developed Europe", F_EUROPE),
    _e("KORU", "etf",  3.0, "South Korea 3x long",
       "Direxion Daily MSCI South Korea Bull 3x — 3x daily long MSCI Korea 25/50", F_KOREA),
    # ---- Single-stock lines (asymmetric +2x bull / -1x bear, like BITX/BITI).
    # Same single-COMPANY event-risk caveat as the 2026-06-10 lines.
    _e("PLTU", "etf",  2.0, "PLTR 2x long",
       "Direxion Daily PLTR Bull 2x — 2x daily long Palantir", F_PLTR),
    _e("PLTD", "etf", -1.0, "PLTR 1x short",
       "Direxion Daily PLTR Bear 1x — 1x daily inverse Palantir", F_PLTR),
    _e("AMZU", "etf",  2.0, "AMZN 2x long",
       "Direxion Daily AMZN Bull 2x — 2x daily long Amazon", F_AMZN),
    _e("AMZD", "etf", -1.0, "AMZN 1x short",
       "Direxion Daily AMZN Bear 1x — 1x daily inverse Amazon", F_AMZN),
    _e("GGLL", "etf",  2.0, "GOOGL 2x long",
       "Direxion Daily GOOGL Bull 2x — 2x daily long Alphabet", F_GOOGL),
    _e("GGLS", "etf", -1.0, "GOOGL 1x short",
       "Direxion Daily GOOGL Bear 1x — 1x daily inverse Alphabet", F_GOOGL),
    _e("METU", "etf",  2.0, "META 2x long",
       "Direxion Daily META Bull 2x — 2x daily long Meta Platforms", F_META),
    _e("METD", "etf", -1.0, "META 1x short",
       "Direxion Daily META Bear 1x — 1x daily inverse Meta Platforms", F_META),
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
