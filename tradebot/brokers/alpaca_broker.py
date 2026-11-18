"""Alpaca (US stocks & ETFs) via alpaca-py.

Options:
- ``paper`` (bool, default True): use Alpaca's paper-trading account endpoint.
  ``sandbox`` is accepted as an alias.
- ``feed`` (str, default "iex"): market-data feed ("iex" is free; "sip" needs a subscription).
- ``adjustment`` (str, default "split"): bar adjustment ("raw", "split", "dividend", "all").
- ``fill_timeout`` (float seconds, default 3): after a market order, poll briefly for its fill.

Credentials: ``api_key``, ``api_secret`` – required for market data as well.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass as AlpacaAssetClass
from alpaca.trading.enums import AssetStatus, OrderSide, TimeInForce
from alpaca.trading.requests import GetAssetsRequest, LimitOrderRequest, MarketOrderRequest

from tradebot.brokers._utils import (
    empty_ohlcv,
    normalize_ohlcv,
    slice_range,
    timeframe_seconds,
    to_utc,
)
from tradebot.brokers.base import (
    AuthenticationError,
    BadSymbolError,
    Broker,
    BrokerError,
    BrokerInfo,
    CredentialField,
    InsufficientFundsError,
    InvalidOrderError,
    NetworkError,
    RateLimitError,
)
from tradebot.core.models import AssetClass, Order, OrderStatus, OrderType, Side

log = logging.getLogger(__name__)

TIMEFRAME_MAP: dict[str, tuple[int, TimeFrameUnit]] = {
    "1m": (1, TimeFrameUnit.Minute),
    "5m": (5, TimeFrameUnit.Minute),
    "15m": (15, TimeFrameUnit.Minute),
    "30m": (30, TimeFrameUnit.Minute),
    "1h": (1, TimeFrameUnit.Hour),
    "4h": (4, TimeFrameUnit.Hour),
    "1d": (1, TimeFrameUnit.Day),
    "1w": (1, TimeFrameUnit.Week),
}

_STATUS_MAP = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "accepted_for_bidding": OrderStatus.NEW,
    "held": OrderStatus.NEW,
    "calculated": OrderStatus.NEW,
    "pending_review": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.CANCELED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.NEW,
    "pending_replace": OrderStatus.NEW,
    "stopped": OrderStatus.NEW,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
}

_DEFAULT_CANDLES = 500


def _enum_value(v) -> str:
    return str(getattr(v, "value", v) or "").lower()


def translate_alpaca_error(exc: Exception) -> BrokerError:
    if isinstance(exc, BrokerError):
        return exc
    if isinstance(exc, APIError):
        status = None
        try:
            status = exc.status_code
        except Exception:  # noqa: BLE001
            pass
        try:
            msg = exc.message
        except Exception:  # noqa: BLE001
            msg = str(exc)
        low = str(msg).lower()
        if "insufficient" in low:
            return InsufficientFundsError(f"Alpaca: Nicht genügend Kaufkraft bzw. Stückzahl. ({msg})")
        if status in (401, 403) and ("forbidden" in low or "unauthorized" in low or status == 401):
            return AuthenticationError(
                f"Alpaca: Authentifizierung fehlgeschlagen – API-Key/Secret und "
                f"Papier/Live-Einstellung prüfen. ({msg})"
            )
        if status == 404 or "not found" in low:
            return BadSymbolError(f"Alpaca: Nicht gefunden (Symbol oder Order). ({msg})")
        if status == 422 or status == 400:
            return InvalidOrderError(f"Alpaca: Anfrage abgelehnt. ({msg})")
        if status == 429:
            return RateLimitError(f"Alpaca: Zu viele Anfragen – bitte kurz warten. ({msg})")
        if status == 403:
            return AuthenticationError(f"Alpaca: Zugriff verweigert. ({msg})")
        return BrokerError(f"Alpaca: API-Fehler (HTTP {status}). ({msg})")
    if isinstance(exc, requests.exceptions.RequestException):
        return NetworkError(f"Alpaca: Netzwerkfehler oder Server nicht erreichbar. ({exc})")
    if isinstance(exc, ValueError):
        return InvalidOrderError(f"Alpaca: Ungültige Anfrage. ({exc})")
    return BrokerError(f"Alpaca: Unerwarteter Fehler: {exc!r}")


class AlpacaBroker(Broker):
    def __init__(self, credentials: dict | None = None, options: dict | None = None) -> None:
        super().__init__(credentials, options)
        self.paper = bool(self.options.get("paper", self.options.get("sandbox", True)))
        self.feed = str(self.options.get("feed", "iex")).lower()
        self.adjustment = str(self.options.get("adjustment", "split")).lower()
        self.fill_timeout = float(self.options.get("fill_timeout", 3.0))
        self._data_client: StockHistoricalDataClient | None = None
        self._trading_client: TradingClient | None = None
        self._symbols: list[str] | None = None

    @classmethod
    def info(cls) -> BrokerInfo:
        return BrokerInfo(
            key="alpaca",
            name="Alpaca",
            asset_classes=[AssetClass.STOCK, AssetClass.ETF],
            supports_trading=True,
            requires_credentials_for_data=True,
            credential_fields=[
                CredentialField("api_key", "API-Key-ID", secret=False),
                CredentialField("api_secret", "Secret Key", secret=True),
            ],
            has_sandbox=True,
            quote_currency="USD",
            description=(
                "US-Aktien und ETFs (provisionsfrei, Bruchstücke möglich). Benötigt API-Keys "
                "auch für Kursdaten (kostenloser IEX-Feed). Papier-Konto verfügbar."
            ),
        )

    # ---- clients --------------------------------------------------------------------
    def _keys(self) -> tuple[str, str]:
        key = (self.credentials.get("api_key") or "").strip()
        secret = (self.credentials.get("api_secret") or "").strip()
        if not key or not secret:
            raise AuthenticationError(
                "Alpaca: Keine API-Zugangsdaten hinterlegt (API-Key-ID und Secret Key "
                "werden auch für Kursdaten benötigt)."
            )
        return key, secret

    @property
    def data_client(self) -> StockHistoricalDataClient:
        if self._data_client is None:
            key, secret = self._keys()
            self._data_client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        return self._data_client

    @property
    def trading_client(self) -> TradingClient:
        if self._trading_client is None:
            key, secret = self._keys()
            self._trading_client = TradingClient(api_key=key, secret_key=secret, paper=self.paper)
        return self._trading_client

    def _wrap(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise translate_alpaca_error(exc) from exc

    def _feed(self) -> DataFeed:
        try:
            return DataFeed(self.feed)
        except ValueError:
            raise BrokerError(f"Alpaca: Unbekannter Daten-Feed '{self.feed}'.") from None

    # ---- market data ----------------------------------------------------------------
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        tf_sec = timeframe_seconds(timeframe)
        amount, unit = TIMEFRAME_MAP[timeframe]
        start, end = to_utc(start), to_utc(end)
        now = datetime.now(timezone.utc)
        keep_head = start is not None and end is None
        n = limit or _DEFAULT_CANDLES
        if start is None:
            ref_end = end or now
            # Markets trade ~6.5h/day, 5 days/week: look back generously, cut with tail().
            span = timedelta(seconds=tf_sec * n)
            factor = 1.6 if tf_sec >= 86400 else 5.0
            start = ref_end - span * factor - timedelta(days=5)
        req_kwargs = dict(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(amount, unit),
            start=start,
            feed=self._feed(),
            adjustment=Adjustment(self.adjustment),
        )
        if end is not None:
            req_kwargs["end"] = end
        if keep_head and limit:
            req_kwargs["limit"] = limit
        req = self._wrap(StockBarsRequest, **req_kwargs)
        barset = self._wrap(self.data_client.get_stock_bars, req)
        data = getattr(barset, "data", None) or {}
        bars = data.get(symbol) or []
        if not bars:
            return empty_ohlcv()
        df = pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=pd.DatetimeIndex([pd.Timestamp(b.timestamp) for b in bars]),
        )
        df = slice_range(normalize_ohlcv(df), start, end)
        if limit or (end is None and start is not None and not keep_head):
            df = df.head(n) if keep_head else df.tail(n)
        return df

    def fetch_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self._feed())
        res = self._wrap(self.data_client.get_stock_latest_trade, req)
        trade = res.get(symbol) if isinstance(res, dict) else None
        if trade is None or not getattr(trade, "price", None):
            raise BadSymbolError(f"Alpaca: Kein aktueller Kurs für {symbol} verfügbar.")
        return float(trade.price)

    def list_symbols(self) -> list[str]:
        if self._symbols is None:
            req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AlpacaAssetClass.US_EQUITY)
            assets = self._wrap(self.trading_client.get_all_assets, req) or []
            self._symbols = sorted(a.symbol for a in assets if getattr(a, "tradable", False))
        return list(self._symbols)

    # ---- account --------------------------------------------------------------------
    def test_connection(self) -> str:
        acc = self._wrap(self.trading_client.get_account)
        mode = "Papier-Konto" if self.paper else "Live-Konto"
        return (f"Verbindung zu Alpaca ({mode}) erfolgreich – Kontostand "
                f"{float(acc.cash):.2f} USD.")

    def fetch_balance(self) -> dict[str, float]:
        acc = self._wrap(self.trading_client.get_account)
        out: dict[str, float] = {"USD": float(acc.cash or 0.0)}
        for p in self._wrap(self.trading_client.get_all_positions) or []:
            qty = getattr(p, "qty_available", None)
            qty = float(qty if qty not in (None, "") else p.qty)
            if qty:
                out[p.symbol] = qty
        return out

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
        qty = self.normalize_quantity(symbol, quantity)
        if qty <= 0:
            raise InvalidOrderError(f"Alpaca: Ungültige Menge {quantity}.")
        a_side = OrderSide.BUY if side == Side.BUY else OrderSide.SELL
        if order_type == OrderType.MARKET:
            req = self._wrap(MarketOrderRequest, symbol=symbol, qty=qty, side=a_side,
                             time_in_force=TimeInForce.DAY)
        else:
            if not price or price <= 0:
                raise InvalidOrderError("Limit-Order benötigt einen gültigen Limitpreis.")
            limit_price = round(float(price), 2 if price >= 1 else 4)
            req = self._wrap(LimitOrderRequest, symbol=symbol, qty=qty, side=a_side,
                             time_in_force=TimeInForce.DAY, limit_price=limit_price)
        res = self._wrap(self.trading_client.submit_order, req)
        if order_type == OrderType.MARKET and self.fill_timeout > 0:
            res = self._wait_for_fill(res)
        return self._map_order(res, symbol, side, qty, order_type, price)

    def _wait_for_fill(self, res):
        deadline = time.monotonic() + self.fill_timeout
        while _STATUS_MAP.get(_enum_value(res.status), OrderStatus.NEW) == OrderStatus.NEW \
                and time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                res = self.trading_client.get_order_by_id(res.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Alpaca get_order_by_id failed: %s", exc)
                break
        return res

    def _map_order(self, res, symbol: str, side: Side, qty: float,
                   order_type: OrderType, req_price: float | None) -> Order:
        filled = float(getattr(res, "filled_qty", None) or 0.0)
        avg = getattr(res, "filled_avg_price", None)
        avg = float(avg) if avg else (float(res.limit_price) if getattr(res, "limit_price", None)
                                      else req_price)
        status = _STATUS_MAP.get(_enum_value(getattr(res, "status", "new")), OrderStatus.NEW)
        created = getattr(res, "submitted_at", None) or getattr(res, "created_at", None)
        created = to_utc(created) if isinstance(created, datetime) else datetime.now(timezone.utc)
        raw = res.model_dump() if hasattr(res, "model_dump") else None
        order = Order(
            symbol=symbol,
            side=side,
            quantity=float(getattr(res, "qty", None) or qty),
            order_type=order_type,
            price=avg,
            status=status,
            filled_quantity=filled,
            fee=0.0,  # Alpaca is commission-free (regulatory fees are not reported per order)
            created_at=created,
            raw=raw,
        )
        if getattr(res, "id", None) is not None:
            order.id = str(res.id)
        return order

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self._wrap(self.trading_client.cancel_order_by_id, order_id)

    def normalize_quantity(self, symbol: str, quantity: float) -> float:
        # Alpaca accepts up to 9 decimal places for fractional shares; round down.
        q = float(quantity)
        return float(int(q * 1e9) / 1e9) if q > 0 else 0.0

    def min_order_value(self, symbol: str) -> float:
        return 1.0  # fractional orders require at least 1 USD notional
