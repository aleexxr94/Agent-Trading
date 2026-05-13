"""Stripped-down option-chain helper — v2 stage between strategist and construct.

Replaces the v1 ``lib/options_chain.py`` (deleted in PR #67) with a much
smaller surface. The v1 version pulled full chains across many strikes
and stuffed them into the scenarios prompt, which (a) made the
scenarios payload huge and (b) the scenarios stage was deleted anyway.

v2 needs a much more constrained thing: for each option candidate the
strategist names (``instrument_kind="option_call"`` or ``"option_put"``
on SPY/QQQ/TLT), return the **single nearest tradable OTM contract** at
the target DTE. The constructor then knows the exact strike + premium
+ Greeks to put in portfolio.json — instead of inventing OSI symbols
that fail at order time.

Cost: $0. Alpaca's options endpoints are free.

Failure modes:
  - Alpaca returns no contracts for the underlying/DTE window → the
    candidate is dropped from the chain_lookups payload; the
    constructor skips that candidate at sizing time.
  - Alpaca returns contracts but the spot price is unavailable → ATM
    falls back to the last signals.last_close; if both unavailable,
    drop the candidate.
  - Any other broker error → log on the per-candidate row; drop the
    candidate. Never raises — one bad lookup mustn't kill the cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Literal

from . import state


@dataclass(frozen=True)
class OptionContract:
    osi_symbol: str
    underlying: str
    type: Literal["call", "put"]
    strike: float
    expiry: str          # YYYY-MM-DD
    dte: int
    # Live quote — when Alpaca returns one. premium_paid in the
    # constructor's portfolio uses this if available, else the
    # constructor estimates from spot + delta-band priors.
    premium_estimate: float | None = None
    bid: float | None = None
    ask: float | None = None
    open_interest: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _target_expiry_window(*, target_dte: int = 37, tolerance_days: int = 14) -> tuple[date, date]:
    """30–45 DTE band centred on target_dte (default 37 = monthly).
    Alpaca query takes expiration_date_gte / _lte."""
    today = date.today()
    return today + timedelta(days=max(target_dte - tolerance_days, 1)), \
           today + timedelta(days=target_dte + tolerance_days)


def _nearest_otm_strike(strikes: list[float], *, spot: float, side: str) -> float | None:
    """Pick the strike just outside the money for the requested side.
    Call: smallest strike > spot. Put: largest strike < spot.
    """
    if not strikes or spot <= 0:
        return None
    if side == "call":
        otm = sorted(s for s in strikes if s > spot)
        return otm[0] if otm else None
    # put
    otm = sorted((s for s in strikes if s < spot), reverse=True)
    return otm[0] if otm else None


def _osi(symbol: str, expiry: str, side: str, strike: float) -> str:
    """OCC OSI option symbol — same convention as lib/orders.osi_symbol."""
    from datetime import datetime
    yymmdd = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
    cp = "C" if side == "call" else "P"
    return f"{symbol}{yymmdd}{cp}{int(round(strike * 1000)):08d}"


def lookup_nearest_otm(
    underlying: str,
    *,
    side: Literal["call", "put"],
    spot: float,
    target_dte: int = 37,
    broker=None,
) -> OptionContract | None:
    """Query the broker for the single nearest-OTM tradable contract
    on ``underlying`` at the target DTE. Returns None on any failure.

    Broker is the AlpacaBroker (or compatible); accesses
    ``broker._client.get_option_contracts``. Stubbed brokers without
    that attribute return None (test path).
    """
    if broker is None or not hasattr(broker, "_client"):
        return None
    try:
        from alpaca.trading.enums import AssetStatus, ContractType  # noqa: WPS433
        from alpaca.trading.requests import GetOptionContractsRequest  # noqa: WPS433
    except ImportError:
        return None

    lo, hi = _target_expiry_window(target_dte=target_dte)
    try:
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            type=ContractType.CALL if side == "call" else ContractType.PUT,
            expiration_date_gte=lo,
            expiration_date_lte=hi,
            limit=500,
        )
        resp = broker._client.get_option_contracts(req)
    except Exception:
        return None

    # alpaca-py returns OptionContractsResponse with .option_contracts
    contracts = getattr(resp, "option_contracts", None) or []
    if not contracts:
        return None

    # Group by expiry, find expiry with most strikes near spot. We want
    # standard monthlies (3rd-Friday) for liquidity — they typically
    # have the widest strike grid, so a "more strikes" tiebreaker
    # implicitly prefers them.
    by_expiry: dict[str, list] = {}
    for c in contracts:
        exp = getattr(c, "expiration_date", None)
        if exp is None:
            continue
        exp_iso = str(exp)
        by_expiry.setdefault(exp_iso, []).append(c)

    # Sort expiries: closest to target_dte first; tiebreak by strike count.
    from datetime import datetime
    today_d = date.today()

    def _dte(exp_iso: str) -> int:
        try:
            return (datetime.strptime(exp_iso, "%Y-%m-%d").date() - today_d).days
        except Exception:
            return 9999

    ranked = sorted(
        by_expiry.keys(),
        key=lambda e: (abs(_dte(e) - target_dte), -len(by_expiry[e])),
    )

    for exp_iso in ranked:
        strikes = []
        for c in by_expiry[exp_iso]:
            try:
                strikes.append(float(getattr(c, "strike_price", 0)))
            except (TypeError, ValueError):
                continue
        otm_strike = _nearest_otm_strike(strikes, spot=spot, side=side)
        if otm_strike is None:
            continue
        # Found it. Build the OSI and return — the contract object's
        # ``symbol`` attr is also Alpaca-native OSI, but recompute so
        # the format is identical to lib/orders.osi_symbol.
        osi = _osi(underlying, exp_iso, side, otm_strike)
        # Locate the contract row for OI lookup.
        match = next(
            (c for c in by_expiry[exp_iso] if abs(float(getattr(c, "strike_price", 0)) - otm_strike) < 0.01),
            None,
        )
        oi = None
        if match is not None:
            try:
                oi = int(getattr(match, "open_interest", 0) or 0)
            except (TypeError, ValueError):
                oi = None
        return OptionContract(
            osi_symbol=osi,
            underlying=underlying,
            type=side,
            strike=otm_strike,
            expiry=exp_iso,
            dte=_dte(exp_iso),
            premium_estimate=None,  # bid/ask requires market-data API; left None
            bid=None,
            ask=None,
            open_interest=oi,
        )
    return None


def lookup_for_view(view: dict, signals_out: dict, *, broker=None) -> dict:
    """Build chain_lookups.json from view + signals.

    Iterates view.candidates, filters to option_call / option_put kinds,
    looks up the nearest OTM contract for each. Returns:

        {
          "run_id": "...",
          "generated_at": "...",
          "lookups": [
            {
              "candidate": {symbol, instrument_kind, confidence},
              "contract": {...} | null,
              "error": "..." | null
            }, ...
          ]
        }

    The constructor reads ``chain_lookups.json`` and consumes the
    ``contract`` field for each option candidate it picks. ETF
    candidates are absent from this payload.
    """
    spot_by_sym = {
        t["symbol"]: t.get("last_close")
        for t in signals_out.get("tickers", [])
        if t.get("last_close") is not None
    }

    lookups: list[dict] = []
    for cand in view.get("candidates", []):
        kind = cand.get("instrument_kind")
        if kind not in ("option_call", "option_put"):
            continue
        sym = cand.get("symbol")
        if not sym:
            continue
        side = "call" if kind == "option_call" else "put"
        spot = spot_by_sym.get(sym)
        if spot is None or spot <= 0:
            lookups.append({
                "candidate": {
                    "symbol": sym,
                    "instrument_kind": kind,
                    "confidence": cand.get("confidence"),
                },
                "contract": None,
                "error": f"no spot price for {sym}",
            })
            continue

        contract = lookup_nearest_otm(
            sym, side=side, spot=float(spot), broker=broker,
        )
        lookups.append({
            "candidate": {
                "symbol": sym,
                "instrument_kind": kind,
                "confidence": cand.get("confidence"),
            },
            "contract": contract.to_dict() if contract is not None else None,
            "error": None if contract is not None else "no tradable OTM contract found",
        })

    return {
        "run_id": view.get("run_id", ""),
        "generated_at": state.utcnow_iso(),
        "lookups": lookups,
    }
