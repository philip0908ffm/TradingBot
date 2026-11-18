"""Crypto spot brokers based on ccxt (Binance, Kraken).

``CcxtBroker`` wraps one ccxt exchange instance; subclasses only set the exchange id and
the registry metadata. All ccxt exceptions are translated into ``BrokerError`` subclasses
with German messages.

Options (``options`` dict):
- ``sandbox`` (bool): use the exchange's test environment (Binance Spot Testnet).
- ``public_data_mirror`` (bool, Binance only): route public market-data endpoints to
  ``https://data-api.binance.vision`` (useful where api.binance.com is geo-blocked).
  Private endpoints (balance, orders) are not affected. Ignored in sandbox mode.
- ``timeout_ms`` (int): HTTP timeout, default 20000.
- ``trust_env`` (bool, default True): use proxy / CA-bundle settings from the environment.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd

from tradebot.brokers._utils import (
    empty_ohlcv,
    ohlcv_from_rows,
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
    NotSupportedError,
    RateLimitError,
)
from tradebot.core.models import AssetClass, Order, OrderStatus, OrderType, Side

log = logging.getLogger(__name__)

BINANCE_PUBLIC_MIRROR = "https://data-api.binance.vision"

_DEFAULT_CANDLES = 500
_MAX_PAGES = 5000  # safety net against endless pagination loops


def translate_ccxt_error(exc: Exception, name: str = "Börse") -> BrokerError:
    """Map a ccxt exception to a BrokerError subclass with a German message."""
    detail = str(exc).strip()
    if len(detail) > 300:
        detail = detail[:300] + "…"
    if isinstance(exc, BrokerError):
        return exc
    if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.AccountSuspended)):
        return AuthenticationError(
            f"{name}: Authentifizierung fehlgeschlagen – bitte API-Key, Secret und "
            f"Berechtigungen prüfen. ({detail})"
        )
    if isinstance(exc, ccxt.InsufficientFunds):
        return InsufficientFundsError(f"{name}: Nicht genügend Guthaben für diese Order. ({detail})")
    if isinstance(exc, ccxt.OrderNotFound):
        return BrokerError(f"{name}: Order nicht gefunden. ({detail})")
    if isinstance(exc, ccxt.InvalidOrder):
        return InvalidOrderError(
            f"{name}: Order ungültig (z. B. Menge, Preis oder Mindestbetrag). ({detail})"
        )
    if isinstance(exc, ccxt.BadSymbol):
        return BadSymbolError(f"{name}: Unbekanntes oder nicht handelbares Symbol. ({detail})")
    if isinstance(exc, ccxt.NotSupported):
        return NotSupportedError(f"{name}: Funktion wird nicht unterstützt. ({detail})")
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        if "451" in detail:
            return NetworkError(
                f"{name}: Zugriff aus dieser Region gesperrt (HTTP 451). ({detail})"
            )
        return RateLimitError(f"{name}: Zu viele Anfragen – bitte kurz warten. ({detail})")
    if isinstance(exc, ccxt.NetworkError):
        if "451" in detail:
            return NetworkError(
                f"{name}: Zugriff aus dieser Region gesperrt (HTTP 451). ({detail})"
            )
        return NetworkError(f"{name}: Netzwerkfehler oder Börse nicht erreichbar. ({detail})")
    if isinstance(exc, ccxt.BaseError):
        return BrokerError(f"{name}: Fehler der Börse. ({detail})")
    return BrokerError(f"{name}: Unerwarteter Fehler: {exc!r}")


_STATUS_MAP = {
    "open": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


class CcxtBroker(Broker):
    """Generic ccxt spot broker. Subclasses set ``exchange_id`` and ``info()``."""

    exchange_id: str = ""
    page_limit: int = 1000  # candles per OHLCV request

    def __init__(self, credentials: dict | None = None, options: dict | None = None) -> None:
        super().__init__(credentials, options)
        self._exchange: ccxt.Exchange | None = None
        self._symbols: list[str] | None = None

    # ---- exchange construction ------------------------------------------------------
    @property
    def display_name(self) -> str:
        return self.info().name

    @property
    def exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = self._create_exchange()
        return self._exchange

    def _exchange_config(self) -> dict:
        cfg: dict = {
            "enableRateLimit": True,
            "timeout": int(self.options.get("timeout_ms", 20000)),
            "options": {"defaultType": "spot"},
            # Honour system proxy / CA-bundle env vars (HTTPS_PROXY, REQUESTS_CA_BUNDLE).
            "requests_trust_env": bool(self.options.get("trust_env", True)),
        }
        if self.credentials.get("api_key"):
            cfg["apiKey"] = self.credentials["api_key"].strip()
        if self.credentials.get("api_secret"):
            cfg["secret"] = self.credentials["api_secret"].strip()
        return cfg

    def _create_exchange(self) -> ccxt.Exchange:
        try:
            cls = getattr(ccxt, self.exchange_id)
            ex = cls(self._exchange_config())
            if self.options.get("sandbox"):
                ex.set_sandbox_mode(True)
            return ex
        except Exception as exc:  # noqa: BLE001
            raise translate_ccxt_error(exc, self.display_name) from exc

    def _call(self, method: str, *args, **kwargs):
        """Call an exchange method and translate errors."""
        try:
            return getattr(self.exchange, method)(*args, **kwargs)
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise translate_ccxt_error(exc, self.display_name) from exc

    def _require_credentials(self) -> None:
        if not (self.credentials.get("api_key") and self.credentials.get("api_secret")):
            raise AuthenticationError(
                f"{self.display_name}: Keine API-Zugangsdaten hinterlegt "
                "(API-Key und Secret erforderlich)."
            )

    def _market(self, symbol: str) -> dict:
        self._call("load_markets")
        try:
            return self.exchange.market(symbol)
        except Exception as exc:  # noqa: BLE001
            raise translate_ccxt_error(exc, self.display_name) from exc

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
        tf_ms = tf_sec * 1000
        timeframes = getattr(self.exchange, "timeframes", None) or {}
        if timeframes and timeframe not in timeframes:
            raise NotSupportedError(
                f"{self.display_name}: Zeitrahmen '{timeframe}' wird nicht unterstützt."
            )
        start, end = to_utc(start), to_utc(end)
        now = datetime.now(timezone.utc)
        tf_delta = timedelta(seconds=tf_sec)
        keep_head = start is not None and end is None  # limit counts from start

        if start is None and end is None:
            n = limit or _DEFAULT_CANDLES
            if n <= self.page_limit:
                rows = self._call("fetch_ohlcv", symbol, timeframe, limit=n)
                return ohlcv_from_rows(rows).tail(n)
            start = now - tf_delta * (n + 1)
            end = now
        elif start is None:
            start = end - tf_delta * ((limit or _DEFAULT_CANDLES) - 1)
        elif end is None:
            end = min(start + tf_delta * (limit - 1), now) if limit else now

        if start > end:
            raise BrokerError("Ungültiger Zeitraum: Start liegt nach dem Ende.")

        df = slice_range(self._paginate(symbol, timeframe, start, end, tf_ms), start, end)
        if limit:
            df = df.head(limit) if keep_head else df.tail(limit)
        return df

    def _paginate(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                  tf_ms: int) -> pd.DataFrame:
        since = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        now_ms = int(time.time() * 1000)
        rows: list[list] = []
        for _ in range(_MAX_PAGES):
            batch = self._call("fetch_ohlcv", symbol, timeframe, since=since, limit=self.page_limit)
            if not batch:
                break
            rows.extend(batch)
            last_ts = int(batch[-1][0])
            next_since = last_ts + tf_ms
            if last_ts >= end_ms or next_since > now_ms or next_since <= since:
                break
            since = next_since
        else:
            log.warning("OHLCV pagination stopped after %d pages for %s", _MAX_PAGES, symbol)
        if not rows:
            return empty_ohlcv()
        return ohlcv_from_rows(rows)

    def fetch_price(self, symbol: str) -> float:
        t = self._call("fetch_ticker", symbol) or {}
        price = t.get("last") or t.get("close")
        if not price and t.get("bid") and t.get("ask"):
            price = (float(t["bid"]) + float(t["ask"])) / 2
        if not price:
            raise BrokerError(f"{self.display_name}: Kein aktueller Kurs für {symbol} verfügbar.")
        return float(price)

    def list_symbols(self) -> list[str]:
        if self._symbols is None:
            markets = self._call("load_markets") or {}
            self._symbols = sorted(
                s for s, m in markets.items()
                if m.get("spot") and m.get("active") is not False
            )
        return list(self._symbols)

    # ---- account --------------------------------------------------------------------
    def test_connection(self) -> str:
        bal = self.fetch_balance()
        mode = " (Testnet)" if self.options.get("sandbox") else ""
        return f"Verbindung zu {self.display_name}{mode} erfolgreich – {len(bal)} Guthaben-Positionen."

    def fetch_balance(self) -> dict[str, float]:
        self._require_credentials()
        bal = self._call("fetch_balance") or {}
        free = bal.get("free") or {}
        return {cur: float(v) for cur, v in free.items() if v}

    def place_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        self._require_credentials()
        side = Side(side)
        order_type = OrderType(order_type)
        qty = self.normalize_quantity(symbol, quantity)
        if qty <= 0:
            raise InvalidOrderError(
                f"{self.display_name}: Menge {quantity} ist nach Rundung auf die Lot-Größe 0."
            )
        req_price = None
        if order_type == OrderType.LIMIT:
            if not price or price <= 0:
                raise InvalidOrderError("Limit-Order benötigt einen gültigen Limitpreis.")
            try:
                req_price = float(self.exchange.price_to_precision(symbol, price))
            except Exception as exc:  # noqa: BLE001
                raise translate_ccxt_error(exc, self.display_name) from exc
        res = self._call("create_order", symbol, order_type.value, side.value, qty, req_price)
        # Some exchanges return only an id; fetch the full order once if fill info is missing.
        if res and res.get("id") and res.get("filled") is None and res.get("status") is None:
            try:
                res = self._call("fetch_order", res["id"], symbol) or res
            except BrokerError as exc:
                log.warning("fetch_order after create_order failed: %s", exc)
        return self._map_order(res or {}, symbol, side, qty, order_type, req_price)

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self._require_credentials()
        self._call("cancel_order", order_id, symbol)

    # ---- order mapping --------------------------------------------------------------
    def _map_order(self, res: dict, symbol: str, side: Side, qty: float,
                   order_type: OrderType, req_price: float | None) -> Order:
        amount = float(res.get("amount") or qty)
        filled = float(res.get("filled") or 0.0)
        cost = res.get("cost")
        avg = res.get("average")
        if not avg and filled and cost:
            avg = float(cost) / filled
        avg = float(avg) if avg else (float(res["price"]) if res.get("price") else req_price)

        status = _STATUS_MAP.get(str(res.get("status") or "open").lower(), OrderStatus.NEW)
        if status == OrderStatus.FILLED and filled and filled < amount * (1 - 1e-9):
            status = OrderStatus.PARTIALLY_FILLED
        elif status == OrderStatus.NEW and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED

        quote = self._quote_of(symbol)
        base = self._base_of(symbol)
        fee = self._fee_in_quote(res, base, quote, avg)

        created = datetime.now(timezone.utc)
        if res.get("timestamp"):
            created = datetime.fromtimestamp(int(res["timestamp"]) / 1000, tz=timezone.utc)
        order = Order(
            symbol=symbol,
            side=side,
            quantity=amount,
            order_type=order_type,
            price=avg,
            status=status,
            filled_quantity=filled,
            fee=fee,
            created_at=created,
            raw=res,
        )
        if res.get("id") is not None:
            order.id = str(res["id"])
        return order

    def _quote_of(self, symbol: str) -> str:
        return symbol.split("/")[1].split(":")[0] if "/" in symbol else ""

    def _base_of(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def _fee_in_quote(self, res: dict, base: str, quote: str, avg: float | None) -> float:
        fees = res.get("fees") or ([res["fee"]] if res.get("fee") else [])
        total = 0.0
        for f in fees:
            if not f or f.get("cost") in (None, ""):
                continue
            cost = float(f["cost"])
            cur = f.get("currency")
            if not cost:
                continue
            if cur in (None, quote):
                total += cost
            elif cur == base and avg:
                total += cost * avg
            else:
                try:
                    total += cost * self.fetch_price(f"{cur}/{quote}")
                except BrokerError:
                    log.warning("Fee in %s could not be converted to %s; ignored", cur, quote)
        return total

    # ---- precision / limits ---------------------------------------------------------
    def normalize_quantity(self, symbol: str, quantity: float) -> float:
        self._market(symbol)
        try:
            return float(self.exchange.amount_to_precision(symbol, quantity))
        except ccxt.InvalidOrder:
            return 0.0  # below precision -> rounds to zero
        except Exception as exc:  # noqa: BLE001
            raise translate_ccxt_error(exc, self.display_name) from exc

    def min_order_value(self, symbol: str) -> float:
        m = self._market(symbol)
        limits = m.get("limits") or {}
        cost_min = (limits.get("cost") or {}).get("min")
        return float(cost_min) if cost_min else 0.0


class BinanceBroker(CcxtBroker):
    exchange_id = "binance"
    page_limit = 1000

    @classmethod
    def info(cls) -> BrokerInfo:
        return BrokerInfo(
            key="binance",
            name="Binance",
            asset_classes=[AssetClass.CRYPTO],
            supports_trading=True,
            requires_credentials_for_data=False,
            credential_fields=[
                CredentialField("api_key", "API-Key", secret=False),
                CredentialField("api_secret", "API-Secret", secret=True),
            ],
            has_sandbox=True,
            quote_currency="USDT",
            description=(
                "Größte Krypto-Börse (Spot-Handel). Kursdaten ohne Zugangsdaten; für den "
                "Handel API-Key mit Spot-Handelsrecht (ohne Auszahlungsrecht!). "
                "Testnet verfügbar (testnet.binance.vision)."
            ),
        )

    def _exchange_config(self) -> dict:
        cfg = super()._exchange_config()
        if self.options.get("public_data_mirror") and not self.options.get("sandbox"):
            # Only spot markets (futures endpoints are not mirrored); no currency lookup
            # via sapi (needs keys + api.binance.com).
            cfg["options"].update({"fetchMarkets": {"types": ["spot"]}, "fetchCurrencies": False})
        return cfg

    def _create_exchange(self) -> ccxt.Exchange:
        ex = super()._create_exchange()
        if self.options.get("public_data_mirror") and not self.options.get("sandbox"):
            api = ex.urls["api"]
            api["public"] = f"{BINANCE_PUBLIC_MIRROR}/api/v3"
            api["v1"] = f"{BINANCE_PUBLIC_MIRROR}/api/v1"
        return ex


class KrakenBroker(CcxtBroker):
    exchange_id = "kraken"
    page_limit = 720  # Kraken returns at most the last 720 candles per timeframe

    @classmethod
    def info(cls) -> BrokerInfo:
        return BrokerInfo(
            key="kraken",
            name="Kraken",
            asset_classes=[AssetClass.CRYPTO],
            supports_trading=True,
            requires_credentials_for_data=False,
            credential_fields=[
                CredentialField("api_key", "API-Key", secret=False),
                CredentialField("api_secret", "Private Key", secret=True),
            ],
            has_sandbox=False,
            quote_currency="EUR",
            description=(
                "Etablierte Krypto-Börse mit EUR-Paaren (z. B. BTC/EUR). Kursdaten ohne "
                "Zugangsdaten; Historie pro Zeitrahmen auf die letzten 720 Kerzen begrenzt. "
                "Kein Testnet für Spot."
            ),
        )
