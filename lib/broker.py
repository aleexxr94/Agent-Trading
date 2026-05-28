"""Broker interface — the only place orchestrator.py talks to a broker.

Concrete implementation lives in lib/alpaca_client.py. This indirection is
deliberate: spec §Critical preconditions #2 mandates that swapping to IBKR
later is a one-file change.

TODO(ibkr): implement lib/ibkr_client.py:IBKRBroker(Broker) when the user
confirms a UK-suitable broker for live trading. Do not implement live
trading until §Promotion to live in CLAUDE.md is satisfied.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
OrderTIF = Literal["day", "gtc"]


@dataclass(frozen=True)
class Account:
    cash_usd: float
    equity_usd: float
    buying_power_usd: float
    is_paper: bool


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    avg_cost: float
    market_value: float
    unrealized_pl_usd: float
    asset_class: Literal["us_equity", "us_option"]
    # Optional Alpaca-derived fields — present when the broker reports them,
    # None for stub / test fixtures that don't bother. See lib/marks.py for
    # why we prefer current_price over deriving it from market_value/qty.
    current_price: float | None = None
    qty_available: float | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    qty: float
    side: OrderSide
    order_type: OrderType = "market"
    limit_price: float | None = None
    tif: OrderTIF = "day"
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    broker_order_id: str
    symbol: str
    qty: float
    side: OrderSide
    submitted_at: str
    status: str


@dataclass(frozen=True)
class MarketClock:
    """Broker-reported market state, used by lib/market_gate.

    `next_open` / `next_close` are ISO-8601 UTC strings when known.
    `timestamp` is the broker's server time as ISO-8601 UTC. Empty string
    is allowed when a field is irrelevant (e.g. ``next_open`` while the
    market IS currently open) — callers must not assume non-empty.
    """
    is_open: bool
    next_open: str
    next_close: str
    timestamp: str


class Broker(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_all(self) -> int: ...

    @abstractmethod
    def flatten(self, symbol: str) -> OrderResult | None: ...

    def get_clock(self) -> MarketClock | None:
        """Return the broker's market-clock snapshot, or None if unsupported.

        Used by lib/market_gate to short-circuit the orchestrator pipeline
        when markets are closed (weekends, holidays, after-hours). The
        default returns None — concrete brokers must override.
        """
        return None

    def option_contract_tradable(self, symbol: str) -> bool:
        """Return True iff the OSI option contract exists and is tradable.

        Default implementation returns True — i.e. no validation. Concrete
        brokers should override to actually query their contract catalog so
        we can skip orders for non-existent / non-tradable contracts before
        the request hits the API. Phase 10d guard: the constructor agent
        sometimes invents OSI symbols whose expiry/strike combo isn't in
        the broker's book (observed May 12 2026: TLT 2026-06-19 P88 returned
        Alpaca error[42210000] 'asset not found'). Pre-validating gives the
        operator a clearer skip reason than the raw broker error and avoids
        burning an order attempt on a doomed contract.
        """
        return True

    def get_option_quote(self, osi_symbol: str) -> tuple[float, float] | None:
        """Return (bid, ask) for an OSI option symbol, or None on any failure.

        Used by lib/options_chain after the nearest-OTM contract is picked
        so the constructor can size against the real mid premium instead
        of priors. Returning None must be safe — the constructor falls
        back to estimating from underlying HV when the quote is missing.

        Default implementation returns None — concrete brokers override.
        Failure modes that should return None (never raise):
          - any HTTP / auth / network error
          - the symbol exists but the feed returned no quote (rare; can
            happen pre-market on illiquid strikes)
          - non-numeric bid/ask in the response
        """
        return None

    def get_underlying_price(self, symbol: str) -> float | None:
        """Return the latest price for an equity/ETF underlying, or None.

        Used by monitor.py to evaluate option positions' underlying price
        stops (underlying_price_below/above), which reference the underlying
        (e.g. SPY), not the option mark. Returning None is safe — the monitor
        simply skips the price stop for that option (its loss cap + time stop
        still apply). Default returns None; concrete brokers override.
        """
        return None
