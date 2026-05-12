"""Option-chain snapshot fetcher — feed real bid/ask/IV/greeks into scenarios.

Phase 9b motivation: prior to this module the scenarios agent quoted
premiums and IVs out of training data, which on a small paper account ran
5-10x off real market prices (May 11 2026: agent priced SPY 565P at
$3.50; actual fill was $0.61). The agent's expected-value math was junk
as a result, and constructor sizing was inflated.

This module:
  1. Parses OCC OSI option symbols.
  2. Normalises Alpaca's OptionsSnapshot dict response into ChainSnapshot
     records that are easy to JSON-serialise into a stage artifact.
  3. Filters a raw chain down to the strikes/expiries worth showing the
     scenarios agent (ATM band + DTE band + spread liquidity).
  4. Provides a thin ChainFetcher wrapper that lazy-imports alpaca-py so
     unit tests and dry-runs don't pay for the SDK.

Only this module and lib/alpaca_client.py import alpaca-py — the IBKR
swap path stays a one-file change (CLAUDE.md §Critical preconditions #2),
with an analogue ChainFetcher behind the same dataclass surface.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Iterable, Literal

from . import options as _options

OptionType = Literal["call", "put"]

_OSI_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class ChainSnapshot:
    osi: str
    underlying: str
    type: OptionType
    strike: float
    expiry: str               # ISO YYYY-MM-DD
    dte: int
    bid: float
    ask: float
    mid: float
    spread_pct: float
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_osi(osi: str) -> tuple[str, str, OptionType, float]:
    """Decode an OCC OSI option symbol → (underlying, expiry, type, strike).

    Examples:
        parse_osi("SPY260619C00530000") → ("SPY", "2026-06-19", "call", 530.0)
        parse_osi("TLT260619P00088000") → ("TLT", "2026-06-19", "put", 88.0)

    Raises ValueError on a non-OSI input.
    """
    m = _OSI_RE.match(osi)
    if not m:
        raise ValueError(f"not a valid OSI symbol: {osi!r}")
    und, yymmdd, cp, strike_raw = m.groups()
    expiry = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return und, expiry, ("call" if cp == "C" else "put"), int(strike_raw) / 1000.0


def _maybe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def snapshots_from_alpaca_chain(
    raw_chain: dict,
    *,
    underlying: str,
    today: date,
) -> list[ChainSnapshot]:
    """Convert Alpaca's option-chain response (mapping OSI → OptionsSnapshot)
    into ChainSnapshot records.

    Filters out:
      - OSIs that don't decode (defensive — Alpaca occasionally returns
        contract symbols we can't parse)
      - OSIs whose underlying != requested underlying (Alpaca sometimes
        returns adjusted-symbol contracts mixed in)
      - rows missing a usable bid AND ask (zero-quote / dead strikes)

    Greeks / IV are best-effort — Alpaca returns null on contracts with
    insufficient data; we preserve None in those cases so the downstream
    prompt can render "n/a" instead of a misleading zero.
    """
    out: list[ChainSnapshot] = []
    for osi, snap in raw_chain.items():
        try:
            und, expiry, type_, strike = parse_osi(osi)
        except ValueError:
            continue
        if und != underlying:
            continue
        try:
            expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (expiry_date - today).days
        if dte < 0:
            continue
        q = getattr(snap, "latest_quote", None)
        bid = _maybe_float(getattr(q, "bid_price", None)) if q is not None else None
        ask = _maybe_float(getattr(q, "ask_price", None)) if q is not None else None
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = 0.5 * (bid + ask)
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else float("inf")
        gr = getattr(snap, "greeks", None)
        out.append(ChainSnapshot(
            osi=osi,
            underlying=und,
            type=type_,
            strike=strike,
            expiry=expiry,
            dte=dte,
            bid=bid,
            ask=ask,
            mid=mid,
            spread_pct=spread_pct,
            iv=_maybe_float(getattr(snap, "implied_volatility", None)),
            delta=_maybe_float(getattr(gr, "delta", None)) if gr is not None else None,
            gamma=_maybe_float(getattr(gr, "gamma", None)) if gr is not None else None,
            theta=_maybe_float(getattr(gr, "theta", None)) if gr is not None else None,
            vega=_maybe_float(getattr(gr, "vega", None)) if gr is not None else None,
        ))
    return out


def filter_chain(
    snaps: Iterable[ChainSnapshot],
    *,
    spot: float,
    min_dte: int = 14,
    max_dte: int = 75,
    atm_band_pct: float = 25.0,
    max_spread_pct: float = 25.0,
) -> list[ChainSnapshot]:
    """Reduce a raw chain to the agent-worthy subset.

    Defaults match the strike/DTE guidance in prompts/scenarios.md:
      - 14-75 DTE band (covers the 30-60 DTE long-put sweet spot plus
        slack on either side for catalyst-driven shorter holds and slow
        themes).
      - ATM ±25% strike band (deep ITM/OTM rarely add information; the
        long-put sweet spot of 0.30-0.40 delta usually sits within ±15%
        but ±25% leaves room for the bear-case bull cases).
      - max spread 25% of mid (filters dead-zone strikes whose marks
        the agent shouldn't trust).
    """
    band = spot * (atm_band_pct / 100.0)
    lo, hi = spot - band, spot + band
    kept: list[ChainSnapshot] = []
    for s in snaps:
        if not (min_dte <= s.dte <= max_dte):
            continue
        if not (lo <= s.strike <= hi):
            continue
        if s.spread_pct > max_spread_pct:
            continue
        kept.append(s)
    return kept


def summarise_chain(snaps: list[ChainSnapshot], *, spot: float) -> dict:
    """Pack a filtered chain into a JSON-friendly summary for the scenarios
    prompt.

    Output shape (per underlying):
      {
        "underlying": "SPY",
        "spot": 530.42,
        "as_of": "2026-05-12T14:30:00Z",
        "calls": [ {osi, strike, expiry, dte, bid, ask, mid, iv, delta, ...}, ... ],
        "puts":  [ ...same shape... ],
      }
    """
    if not snaps:
        return {"underlying": None, "spot": spot, "calls": [], "puts": []}
    calls = sorted(
        (s for s in snaps if s.type == "call"),
        key=lambda s: (s.dte, s.strike),
    )
    puts = sorted(
        (s for s in snaps if s.type == "put"),
        key=lambda s: (s.dte, s.strike),
    )
    return {
        "underlying": snaps[0].underlying,
        "spot": spot,
        "calls": [s.to_dict() for s in calls],
        "puts": [s.to_dict() for s in puts],
    }


class ChainFetchError(RuntimeError):
    """Raised when Alpaca's option-chain endpoint fails or returns no
    usable data. Callers should treat this as a soft failure: scenarios
    can still run, just without real chain context for the affected
    underlying."""


class ChainFetcher:
    """Thin Alpaca wrapper for option-chain snapshots.

    Lazy-imports alpaca-py so dry-runs and unit tests don't pay for the
    SDK. The Alpaca data plane is separate from the trading plane —
    OPRA-licensed accounts get live quotes; basic paper accounts get
    15-min delayed indicative quotes (still usable for the agent's
    decisions; we're not arbitraging milliseconds).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        client: Any = None,
    ) -> None:
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("ALPACA_API_KEY", "")
        sec = api_secret or os.environ.get("ALPACA_API_SECRET", "")
        if not key or not sec:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_API_SECRET required for option-chain fetch"
            )
        from alpaca.data.historical.option import OptionHistoricalDataClient
        self._client = OptionHistoricalDataClient(key, sec)

    def fetch(
        self,
        underlying: str,
        *,
        spot: float,
        today: date | None = None,
        min_dte: int = 14,
        max_dte: int = 75,
        atm_band_pct: float = 25.0,
        max_spread_pct: float = 25.0,
    ) -> dict:
        """Fetch + filter + summarise a real chain for ``underlying``.

        Returns the dict shape produced by ``summarise_chain``. On any
        Alpaca-side failure raises ChainFetchError — callers should
        catch and degrade gracefully rather than abort the run. This
        includes the case where ``alpaca-py`` isn't installed: the SDK
        import lives inside the try block so a ModuleNotFoundError
        surfaces through the same soft-failure path rather than
        crashing the pipeline.
        """
        today = today or date.today()
        # Pre-filter on Alpaca's side using strike/expiry bounds so the
        # response is small even on huge chains (SPX, SPY); we still
        # re-filter locally because Alpaca occasionally returns rows
        # outside the bounds (adjusted-strike contracts, etc).
        band = spot * (atm_band_pct / 100.0)
        try:
            from alpaca.data.requests import OptionChainRequest  # noqa: WPS433
            req = OptionChainRequest(
                underlying_symbol=underlying,
                strike_price_gte=max(0.0, spot - band),
                strike_price_lte=spot + band,
                expiration_date_gte=today.fromordinal(today.toordinal() + min_dte),
                expiration_date_lte=today.fromordinal(today.toordinal() + max_dte),
            )
            raw = self._client.get_option_chain(req)
        except Exception as e:
            raise ChainFetchError(
                f"alpaca get_option_chain failed for {underlying}: {e}"
            ) from e
        if not raw:
            raise ChainFetchError(f"empty chain for {underlying}")
        snaps = snapshots_from_alpaca_chain(raw, underlying=underlying, today=today)
        kept = filter_chain(
            snaps,
            spot=spot,
            min_dte=min_dte,
            max_dte=max_dte,
            atm_band_pct=atm_band_pct,
            max_spread_pct=max_spread_pct,
        )
        if not kept:
            raise ChainFetchError(
                f"no liquid strikes for {underlying} within "
                f"DTE [{min_dte},{max_dte}] / ATM ±{atm_band_pct}%"
            )
        return summarise_chain(kept, spot=spot)


# Re-export the liquidity helper so callers wiring custom flows don't need
# to import two modules. Kept as a thin alias rather than a re-import so
# the seam stays obvious.
passes_chain_liquidity = _options.passes_chain_liquidity
