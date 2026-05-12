"""Alpaca paper broker — the only file that imports alpaca-py.

Live trading is gated by env vars and the LIVE_VERSION constant in the
orchestrator (see spec §Critical preconditions #1 and §Promotion to live).
This client refuses to construct against a non-paper base URL unless that
gate is explicitly raised — fail-closed by default.
"""
from __future__ import annotations

import os
from dataclasses import asdict

from .broker import (
    Account,
    Broker,
    BrokerPosition,
    OrderRequest,
    OrderResult,
)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _normalize_asset_class(raw) -> str:
    """Map whatever Alpaca returns into our internal canonical values.

    Per Alpaca docs (2026-Q2 reference): REST asset_class can be
    'us_equity', 'option', or 'crypto'. The alpaca-py SDK enum
    AssetClass.US_OPTION evaluates to 'us_option' (str-Enum mixin).
    Both forms — bare 'option' and 'us_option' — show up in the wild
    depending on whether we're consuming the SDK object or a raw dict.
    Normalise both to 'us_option' so downstream code (lib.marks,
    lib.orders, monitor) only has to check one value.
    """
    s = str(raw).lower()
    if "option" in s:
        return "us_option"
    return "us_equity"


class AlpacaBroker(Broker):
    """Wraps alpaca-py's TradingClient. Paper-only unless LIVE explicitly enabled."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        paper: bool | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._api_secret = api_secret or os.environ.get("ALPACA_API_SECRET", "")
        self._base_url = base_url or os.environ.get("ALPACA_BASE_URL", PAPER_BASE_URL)
        if not self._api_key or not self._api_secret:
            raise RuntimeError(
                "Alpaca credentials missing — set ALPACA_API_KEY and ALPACA_API_SECRET"
            )
        self._paper = self._base_url == PAPER_BASE_URL if paper is None else paper
        if not self._paper and os.environ.get("LIVE_TRADING_ENABLED", "false").lower() != "true":
            raise RuntimeError(
                "Refusing non-paper Alpaca client without LIVE_TRADING_ENABLED=true. "
                "See CLAUDE.md §Promotion to live."
            )
        # Lazy import keeps the dependency optional for unit tests / dry runs.
        from alpaca.trading.client import TradingClient  # noqa: WPS433

        self._client = TradingClient(self._api_key, self._api_secret, paper=self._paper)

    @property
    def name(self) -> str:
        return "alpaca-paper" if self._paper else "alpaca-live"

    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(
            cash_usd=float(a.cash),
            equity_usd=float(a.equity),
            buying_power_usd=float(a.buying_power),
            is_paper=self._paper,
        )

    def get_positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for p in self._client.get_all_positions():
            # current_price + qty_available are optional in alpaca-py's Position
            # model; guard with getattr so we don't crash on older SDK versions
            # or test stubs that haven't bothered.
            cp = getattr(p, "current_price", None)
            qa = getattr(p, "qty_available", None)
            out.append(
                BrokerPosition(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_cost=float(p.avg_entry_price),
                    market_value=float(p.market_value),
                    unrealized_pl_usd=float(p.unrealized_pl),
                    asset_class=_normalize_asset_class(p.asset_class),
                    current_price=float(cp) if cp is not None else None,
                    qty_available=float(qa) if qa is not None else None,
                )
            )
        return out

    def submit_order(self, order: OrderRequest) -> OrderResult:
        from alpaca.trading.enums import OrderSide, OrderType, TimeInForce  # noqa: WPS433
        from alpaca.trading.requests import (  # noqa: WPS433
            LimitOrderRequest,
            MarketOrderRequest,
        )

        side = OrderSide.BUY if order.side == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY if order.tif == "day" else TimeInForce.GTC
        if order.order_type == "limit":
            req = LimitOrderRequest(
                symbol=order.symbol,
                qty=order.qty,
                side=side,
                time_in_force=tif,
                limit_price=order.limit_price,
                client_order_id=order.client_order_id,
            )
        else:
            req = MarketOrderRequest(
                symbol=order.symbol,
                qty=order.qty,
                side=side,
                time_in_force=tif,
                client_order_id=order.client_order_id,
            )
        r = self._client.submit_order(req)
        return OrderResult(
            broker_order_id=str(r.id),
            symbol=r.symbol,
            qty=float(r.qty),
            side=order.side,
            submitted_at=str(r.submitted_at),
            status=str(r.status),
        )

    def option_contract_tradable(self, symbol: str) -> bool:
        """Query Alpaca for a single option contract by OSI symbol.

        Uses TradingClient.get_option_contract(symbol_or_id), which accepts
        the full OSI string and returns the OptionContract or raises a 404.
        Direct lookup bypasses the 50-row pagination of get_option_contracts
        — useful because the constructor sometimes picks an expiry that
        isn't on the first page of the chain listing.

        Returns False on:
          - any exception (404 'asset not found', auth, network)
          - status != 'active'
          - tradable == False
        True only when the contract is present AND marked tradable.
        """
        try:
            c = self._client.get_option_contract(symbol)
        except Exception:
            return False
        status = getattr(c, "status", None)
        tradable = getattr(c, "tradable", None)
        if tradable is False:
            return False
        if status is not None and str(status).lower() != "active":
            return False
        return True

    def cancel_all(self) -> int:
        return len(self._client.cancel_orders())

    def flatten(self, symbol: str) -> OrderResult | None:
        try:
            r = self._client.close_position(symbol)
        except Exception:
            return None
        return OrderResult(
            broker_order_id=str(r.id),
            symbol=symbol,
            qty=float(getattr(r, "qty", 0) or 0),
            side="sell",
            submitted_at=str(getattr(r, "submitted_at", "")),
            status=str(getattr(r, "status", "submitted")),
        )

    # Useful for diagnostics; not part of the Broker interface.
    def diag(self) -> dict:
        return {"name": self.name, **asdict(self.get_account())}
