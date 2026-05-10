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
