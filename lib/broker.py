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
    # The system trades ETFs only. ``us_option`` is retained solely so a
    # stray/legacy option position at the broker can still be ingested and
    # safely flattened (close-only) by monitor.py — it is never opened.
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
