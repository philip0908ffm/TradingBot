"""Paper trading: real market data, virtual money. Nothing is ever sent to a real broker.

``PaperAccount`` is a virtual account (cash + holdings + order history) that can be shared
by several ``PaperBroker`` instances (e.g. one per data source). It is thread-safe and can
persist itself as JSON.

``PaperBroker`` wraps a data broker for prices and simulates fills:
- market orders fill immediately at ``fetch_price`` ± ``slippage_pct``;
- limit orders fill immediately if marketable (buy: limit >= price, sell: limit <= price) at
  the better of slipped market price and limit (buy: min(price*(1+slip), limit),
  sell: max(price*(1-slip), limit)); otherwise they are rejected with
  ``NotSupportedError`` (no resting order book in paper mode);
- a fee of ``fee_pct`` percent of the notional is charged in the cash currency.

Holdings are keyed by the full symbol (e.g. "BTC/USDT", "AAPL"); cash is a single number
regardless of quote currency (USD/USDT/EUR are not converted).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from tradebot.brokers.base import (
    Broker,
    BrokerError,
    BrokerInfo,
    InsufficientFundsError,
    InvalidOrderError,
    NotSupportedError,
)
from tradebot.core.models import AssetClass, Order, OrderStatus, OrderType, Side, utcnow

log = logging.getLogger(__name__)

_EPS = 1e-12


def order_from_dict(d: dict) -> Order:
    """Rebuild an ``Order`` from ``Order.to_dict()`` output."""
    return Order(
        symbol=d["symbol"],
        side=Side(d["side"]),
        quantity=float(d["quantity"]),
        order_type=OrderType(d.get("order_type", "market")),
        price=None if d.get("price") is None else float(d["price"]),
        status=OrderStatus(d.get("status", "filled")),
        filled_quantity=float(d.get("filled_quantity", 0.0)),
        fee=float(d.get("fee", 0.0)),
        id=str(d["id"]),
        created_at=datetime.fromisoformat(d["created_at"]),
    )


class PaperAccount:
    """Virtual account. All mutating methods are atomic (lock) and auto-save if ``path`` set."""

    def __init__(self, starting_cash: float = 10_000.0, path: str | Path | None = None,
                 max_history: int = 5000) -> None:
        self._lock = threading.RLock()
        self.path = Path(path) if path else None
        self.max_history = max_history
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.holdings: dict[str, float] = {}
        self.orders: list[dict] = []
        if self.path and self.path.exists():
            self.load()

    # ---- persistence ----------------------------------------------------------------
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "starting_cash": self.starting_cash,
                "cash": self.cash,
                "holdings": dict(self.holdings),
                "orders": list(self.orders),
            }

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = self.to_dict()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrokerError(f"Papierkonto konnte nicht geladen werden ({self.path}): {exc}") from exc
        with self._lock:
            self.starting_cash = float(data.get("starting_cash", self.starting_cash))
            self.cash = float(data.get("cash", self.starting_cash))
            self.holdings = {k: float(v) for k, v in (data.get("holdings") or {}).items()}
            self.orders = list(data.get("orders") or [])

    # ---- queries / admin ------------------------------------------------------------
    def reset(self, starting_cash: float | None = None) -> None:
        with self._lock:
            if starting_cash is not None:
                self.starting_cash = float(starting_cash)
            self.cash = self.starting_cash
            self.holdings = {}
            self.orders = []
            self.save()

    def equity(self, prices: dict[str, float]) -> float:
        """Cash + market value of holdings (holdings without a price are valued at 0)."""
        with self._lock:
            total = self.cash
            for sym, qty in self.holdings.items():
                p = prices.get(sym)
                if p is None:
                    log.debug("No price for %s in equity()", sym)
                    continue
                total += qty * float(p)
            return total

    def holding(self, symbol: str) -> float:
        with self._lock:
            return self.holdings.get(symbol, 0.0)

    def find_order(self, order_id: str) -> dict | None:
        with self._lock:
            for d in reversed(self.orders):
                if d.get("id") == order_id:
                    return dict(d)
        return None

    # ---- fills ----------------------------------------------------------------------
    def apply_fill(self, order: Order) -> None:
        """Book a filled order (cash, holdings, history). Raises if funds are insufficient."""
        qty = order.filled_quantity
        price = float(order.price or 0.0)
        notional = qty * price
        with self._lock:
            if order.side == Side.BUY:
                needed = notional + order.fee
                if needed > self.cash + 1e-9:
                    raise InsufficientFundsError(
                        f"Papierhandel: Nicht genügend Guthaben – benötigt {needed:.2f}, "
                        f"verfügbar {self.cash:.2f}."
                    )
                self.cash -= needed
                self.holdings[order.symbol] = self.holdings.get(order.symbol, 0.0) + qty
            else:
                have = self.holdings.get(order.symbol, 0.0)
                if qty > have * (1 + 1e-9) + _EPS:
                    raise InsufficientFundsError(
                        f"Papierhandel: Nicht genügend Bestand in {order.symbol} – "
                        f"benötigt {qty:g}, vorhanden {have:g}."
                    )
                rest = have - qty
                if rest <= max(_EPS, have * 1e-9):
                    self.holdings.pop(order.symbol, None)
                else:
                    self.holdings[order.symbol] = rest
                self.cash += notional - order.fee
            self.orders.append(order.to_dict())
            if len(self.orders) > self.max_history:
                del self.orders[: len(self.orders) - self.max_history]
            self.save()


class PaperBroker(Broker):
    """Simulated broker on top of a real data broker."""

    def __init__(self, data_broker: Broker, account: PaperAccount, fee_pct: float = 0.1,
                 slippage_pct: float = 0.05) -> None:
        super().__init__(credentials=None, options={})
        self.data_broker = data_broker
        self.account = account
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)

    @classmethod
    def info(cls) -> BrokerInfo:
        return BrokerInfo(
            key="paper",
            name="Papierhandel (Test)",
            asset_classes=[AssetClass.CRYPTO, AssetClass.STOCK, AssetClass.ETF],
            supports_trading=True,
            requires_credentials_for_data=False,
            credential_fields=[],
            has_sandbox=False,
            quote_currency="USD",
            description=(
                "Virtuelles Konto mit echten Kursen. Orders werden simuliert (Gebühr und "
                "Slippage) und nie an einen echten Broker gesendet."
            ),
        )

    # ---- market data: delegated -----------------------------------------------------
    def fetch_ohlcv(self, symbol, timeframe, start=None, end=None, limit=None) -> pd.DataFrame:
        return self.data_broker.fetch_ohlcv(symbol, timeframe, start=start, end=end, limit=limit)

    def fetch_price(self, symbol: str) -> float:
        return self.data_broker.fetch_price(symbol)

    def list_symbols(self) -> list[str]:
        return self.data_broker.list_symbols()

    def normalize_quantity(self, symbol: str, quantity: float) -> float:
        try:
            return self.data_broker.normalize_quantity(symbol, quantity)
        except BrokerError:
            return quantity

    def min_order_value(self, symbol: str) -> float:
        try:
            return self.data_broker.min_order_value(symbol)
        except BrokerError:
            return 0.0

    # ---- account --------------------------------------------------------------------
    def test_connection(self) -> str:
        return "Papierhandel aktiv – es werden keine echten Orders gesendet."

    def fetch_balance(self) -> dict[str, float]:
        with self.account._lock:
            return {"USD": self.account.cash, **self.account.holdings}

    def place_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        side = Side(side)
        order_type = OrderType(order_type)
        qty = self.normalize_quantity(symbol, float(quantity))
        if qty <= 0:
            raise InvalidOrderError(f"Papierhandel: Ungültige Menge {quantity}.")
        market = float(self.fetch_price(symbol))
        if market <= 0:
            raise BrokerError(f"Papierhandel: Ungültiger Kurs für {symbol}.")
        slip = self.slippage_pct / 100.0
        fill = market * (1 + slip) if side == Side.BUY else market * (1 - slip)

        if order_type == OrderType.LIMIT:
            if not price or price <= 0:
                raise InvalidOrderError("Limit-Order benötigt einen gültigen Limitpreis.")
            marketable = price >= market if side == Side.BUY else price <= market
            if not marketable:
                raise NotSupportedError(
                    "Papierhandel: Nicht sofort ausführbare Limit-Orders werden nicht "
                    f"unterstützt (Limit {price:g}, Kurs {market:g})."
                )
            fill = min(fill, price) if side == Side.BUY else max(fill, price)

        min_value = self.min_order_value(symbol)
        if min_value and qty * fill < min_value:
            raise InvalidOrderError(
                f"Papierhandel: Ordervolumen {qty * fill:.2f} unter dem Mindestbetrag {min_value:g}."
            )
        fee = qty * fill * self.fee_pct / 100.0
        order = Order(
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type=order_type,
            price=fill,
            status=OrderStatus.FILLED,
            filled_quantity=qty,
            fee=fee,
            created_at=utcnow(),
            raw={"paper": True, "market_price": market, "limit_price": price},
        )
        self.account.apply_fill(order)
        return order

    def fetch_order(self, order_id: str, symbol: str) -> Order:
        d = self.account.find_order(order_id)
        if d is None:
            raise BrokerError(f"Papierhandel: Order {order_id} nicht gefunden.")
        return order_from_dict(d)

    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Paper orders fill immediately, so there is never an open remainder to cancel.

        Cancelling a known (already filled) order is a no-op; unknown ids raise.
        """
        if self.account.find_order(order_id) is None:
            raise BrokerError(f"Papierhandel: Order {order_id} nicht gefunden.")
        log.debug("Paper cancel_order(%s): already filled, nothing to cancel", order_id)
