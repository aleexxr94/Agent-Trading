"""Static universe of tradeable instruments for the screener.

Per CLAUDE.md §System scope:
  - Leveraged ETFs (2x/3x equity, sector, vol)
  - Listed options on liquid underlyings (SPY, QQQ, IWM, and high-volume
    leveraged ETFs)
  - **No spot single-name equities. No unleveraged broad-market ETFs as
    core positions.**

This list is curated, not exhaustive. Add/remove entries as the universe
evolves. The screener still applies liquidity filters strictly — anything
that fails ADV / spread checks at runtime is rejected even if listed here.
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

    @property
    def is_inverse(self) -> bool:
        return self.leverage_factor < 0


_LEVERAGED_ETFS: tuple[UniverseEntry, ...] = (
    # ---- Nasdaq-100 ----
    UniverseEntry("TQQQ", "etf",  3.0, "Nasdaq 3x long",
                  "ProShares UltraPro QQQ — 3x daily long Nasdaq-100"),
    UniverseEntry("SQQQ", "etf", -3.0, "Nasdaq 3x short",
                  "ProShares UltraPro Short QQQ — 3x daily inverse Nasdaq-100"),
    # ---- S&P 500 ----
    UniverseEntry("UPRO", "etf",  3.0, "S&P 500 3x long",
                  "ProShares UltraPro S&P500 — 3x daily long S&P 500"),
    UniverseEntry("SPXU", "etf", -3.0, "S&P 500 3x short",
                  "ProShares UltraPro Short S&P500 — 3x daily inverse S&P 500"),
    # ---- Russell 2000 / small caps ----
    UniverseEntry("TNA",  "etf",  3.0, "Russell 2000 3x long",
                  "Direxion Daily Small Cap Bull 3x — 3x daily long Russell 2000"),
    UniverseEntry("TZA",  "etf", -3.0, "Russell 2000 3x short",
                  "Direxion Daily Small Cap Bear 3x — 3x daily inverse Russell 2000"),
    UniverseEntry("URTY", "etf",  3.0, "Russell 2000 3x long (alt)",
                  "ProShares UltraPro Russell2000 — 3x daily long Russell 2000"),
    UniverseEntry("SRTY", "etf", -3.0, "Russell 2000 3x short (alt)",
                  "ProShares UltraPro Short Russell2000 — 3x daily inverse Russell 2000"),
    # ---- Semiconductors ----
    UniverseEntry("SOXL", "etf",  3.0, "Semis 3x long",
                  "Direxion Daily Semiconductor Bull 3x — 3x daily long PHLX Semi"),
    UniverseEntry("SOXS", "etf", -3.0, "Semis 3x short",
                  "Direxion Daily Semiconductor Bear 3x — 3x daily inverse PHLX Semi"),
    # ---- Financials ----
    UniverseEntry("FAS",  "etf",  3.0, "Financials 3x long",
                  "Direxion Daily Financial Bull 3x — 3x daily long Russell 1000 Financials"),
    UniverseEntry("FAZ",  "etf", -3.0, "Financials 3x short",
                  "Direxion Daily Financial Bear 3x — 3x daily inverse Russell 1000 Financials"),
    # ---- Volatility / commodity ----
    UniverseEntry("UVXY", "etf",  1.5, "VIX 1.5x long",
                  "ProShares Ultra VIX Short-Term Futures — 1.5x daily long VIX front-month"),
    UniverseEntry("BOIL", "etf",  2.0, "Natural Gas 2x long",
                  "ProShares Ultra Bloomberg Natural Gas — 2x daily long NatGas futures"),
)


_OPTION_UNDERLYINGS: tuple[UniverseEntry, ...] = (
    UniverseEntry("SPY",  "option_underlying", 1.0, "S&P 500 ETF",
                  "Most liquid options chain in the world"),
    UniverseEntry("QQQ",  "option_underlying", 1.0, "Nasdaq-100 ETF",
                  "Tech-heavy options chain, very liquid"),
    UniverseEntry("IWM",  "option_underlying", 1.0, "Russell 2000 ETF",
                  "Small-cap options chain"),
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
    description. Combined with live data from market_data.universe_snapshot()
    to form the full universe block sent to the screener."""
    return [
        {
            "symbol": e.symbol,
            "kind": e.kind,
            "leverage_factor": e.leverage_factor,
            "family": e.family,
            "description": e.description,
        }
        for e in UNIVERSE
    ]
