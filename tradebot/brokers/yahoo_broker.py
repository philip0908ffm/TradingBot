"""Yahoo Finance (data only) via yfinance.

Stocks, ETFs and crypto (e.g. "AAPL", "SAP.DE", "VWCE.DE", "BTC-USD"). No trading, no
credentials. Yahoo's interval limits:

- 1m: only the last ~30 days, max 7 days per request (requests are chunked).
- 5m/15m/30m: last 60 days.
- 1h: last 730 days. 4h is resampled from 1h.
- 1d / 1w: unlimited.

If the requested start lies before the allowed window it is clamped (with a log warning);
if the whole range lies outside, a ``BrokerError`` is raised.
"""
from __future__ import annotations

import contextlib
import logging
import warnings
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from yfinance import exceptions as yf_exc

from tradebot.brokers._utils import (
    empty_ohlcv,
    normalize_ohlcv,
    slice_range,
    timeframe_seconds,
    to_utc,
)
from tradebot.brokers.base import (
    BadSymbolError,
    Broker,
    BrokerError,
    BrokerInfo,
    NetworkError,
    NotSupportedError,
    RateLimitError,
)
from tradebot.core.models import AssetClass, Order, OrderType, Side

log = logging.getLogger(__name__)

# app timeframe -> (yfinance interval, max lookback or None, max span per request or None)
INTERVALS: dict[str, tuple[str, timedelta | None, timedelta | None]] = {
    "1m": ("1m", timedelta(days=29), timedelta(days=7)),
    "5m": ("5m", timedelta(days=59), None),
    "15m": ("15m", timedelta(days=59), None),
    "30m": ("30m", timedelta(days=59), None),
    "1h": ("1h", timedelta(days=729), None),
    "4h": ("1h", timedelta(days=729), None),  # resampled
    "1d": ("1d", None, None),
    "1w": ("1wk", None, None),
}

POPULAR_SYMBOLS: list[str] = [
    # US mega caps
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "AVGO", "JPM",
    "V", "MA", "UNH", "XOM", "LLY", "JNJ", "PG", "COST", "HD", "NFLX",
    "AMD", "INTC", "KO", "PEP", "DIS",
    # DAX
    "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "AIR.DE", "MBG.DE", "BMW.DE", "VOW3.DE",
    "BAS.DE", "BAYN.DE", "MUV2.DE", "DBK.DE", "IFX.DE", "ADS.DE", "RHM.DE", "DHL.DE",
    # ETFs
    "SPY", "QQQ", "VOO", "VTI", "IWM", "DIA", "GLD", "TLT",
    "VWCE.DE", "EUNL.DE", "IWDA.AS", "EXS1.DE", "SXR8.DE", "EQQQ.DE", "XDWD.DE",
    # Crypto
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BTC-EUR", "ETH-EUR",
]

_DEFAULT_CANDLES = 500


@contextlib.contextmanager
def _quiet():
    """Silence pandas deprecation warnings raised inside yfinance (not our code)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.filterwarnings("ignore", message=".*inplace.*")
        yield


def _translate(exc: Exception, symbol: str) -> BrokerError:
    if isinstance(exc, BrokerError):
        return exc
    if isinstance(exc, yf_exc.YFRateLimitError):
        return RateLimitError("Yahoo Finance: Zu viele Anfragen (Rate-Limit) – bitte später erneut versuchen.")
    if isinstance(exc, (yf_exc.YFTickerMissingError, yf_exc.YFTzMissingError,
                        yf_exc.YFPricesMissingError)):
        return BadSymbolError(f"Yahoo Finance: Keine Daten für '{symbol}' (Symbol unbekannt oder delistet?).")
    name = type(exc).__name__
    if "Connection" in name or "Timeout" in name or "SSL" in name:
        return NetworkError(f"Yahoo Finance: Netzwerkfehler ({exc}).")
    return BrokerError(f"Yahoo Finance: Fehler beim Abruf von '{symbol}': {exc}")


class YahooBroker(Broker):
    @classmethod
    def info(cls) -> BrokerInfo:
        return BrokerInfo(
            key="yahoo",
            name="Yahoo Finance",
            asset_classes=[AssetClass.STOCK, AssetClass.ETF, AssetClass.CRYPTO],
            supports_trading=False,
            requires_credentials_for_data=False,
            credential_fields=[],
            has_sandbox=False,
            quote_currency="USD",
            description=(
                "Kostenlose Kursdaten für Aktien, ETFs und Krypto (z. B. AAPL, SAP.DE, "
                "VWCE.DE, BTC-USD). Nur Daten – kein Handel; ideal für Backtests und "
                "Papierhandel. Intraday-Historie begrenzt (1m: ~30 Tage, 5m–30m: 60 Tage, "
                "1h/4h: 730 Tage)."
            ),
        )

    # ---- market data ----------------------------------------------------------------
    def _history(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        try:
            with _quiet():
                raw = yf.Ticker(symbol).history(
                    start=start, end=end, interval=interval,
                    auto_adjust=True, actions=False, prepost=False,
                )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, symbol) from exc
        if raw is None or raw.empty:
            return empty_ohlcv()
        return raw

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        tf_sec = timeframe_seconds(timeframe)
        interval, max_back, chunk = INTERVALS[timeframe]
        tf_delta = timedelta(seconds=tf_sec)
        now = datetime.now(timezone.utc)
        start, end = to_utc(start), to_utc(end)
        keep_head = start is not None and end is None
        auto_start = start is None
        n = limit or _DEFAULT_CANDLES
        if end is None:
            end = min(start + tf_delta * (limit - 1), now) if (start and limit) else now
        if start is None:
            factor = 1.6 if tf_sec >= 86400 else 5.0  # markets are closed nights/weekends
            start = end - tf_delta * n * factor - timedelta(days=4)
        if start > end:
            raise BrokerError("Ungültiger Zeitraum: Start liegt nach dem Ende.")

        if max_back is not None:
            earliest = now - max_back
            if end < earliest:
                raise BrokerError(
                    f"Yahoo Finance liefert {timeframe}-Kerzen nur für die letzten "
                    f"{max_back.days} Tage. Bitte einen größeren Zeitrahmen (z. B. 1d) "
                    "oder einen jüngeren Zeitraum wählen."
                )
            if start < earliest:
                if not auto_start:
                    log.warning("Yahoo: %s start %s clamped to %s (interval limit)",
                                timeframe, start, earliest)
                start = earliest

        # yfinance's end is exclusive -> extend by one candle
        fetch_end = min(end + tf_delta, now + timedelta(days=1))
        frames = []
        cur = start
        step = chunk or (fetch_end - start)
        while cur < fetch_end:
            nxt = min(cur + step, fetch_end)
            part = self._history(symbol, interval, cur, nxt)
            if not part.empty:
                frames.append(part)
            cur = nxt
        if not frames:
            if auto_start or (end - start) > timedelta(days=7):
                raise BadSymbolError(
                    f"Yahoo Finance: Keine Daten für '{symbol}' im gewählten Zeitraum "
                    "(Symbol prüfen, z. B. 'SAP.DE' oder 'BTC-USD')."
                )
            return empty_ohlcv()
        df = self._normalize(pd.concat(frames), interval)
        if timeframe == "4h":
            df = self._resample(df, "4h")
        df = slice_range(df, start, end)
        if limit:
            df = df.head(limit) if keep_head else df.tail(limit)
        elif auto_start:
            df = df.tail(n)
        return df

    @staticmethod
    def _normalize(raw: pd.DataFrame, interval: str) -> pd.DataFrame:
        df = raw.copy()
        idx = pd.DatetimeIndex(df.index)
        if interval in ("1d", "1wk"):
            # Daily/weekly bars carry the exchange's local midnight -> use the date at 00:00 UTC
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            idx = idx.normalize().tz_localize("UTC")
        df.index = idx
        df = normalize_ohlcv(df)
        return df.dropna(subset=["open", "high", "low", "close"])

    @staticmethod
    def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.resample(rule, origin="epoch", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
        return out.dropna(subset=["open", "close"])

    def fetch_price(self, symbol: str) -> float:
        price = None
        try:
            with _quiet():
                price = yf.Ticker(symbol).fast_info["last_price"]
        except yf_exc.YFRateLimitError as exc:
            raise _translate(exc, symbol) from exc
        except Exception as exc:  # noqa: BLE001
            log.debug("fast_info failed for %s: %s", symbol, exc)
        if not price or price != price:  # None / 0 / NaN
            df = self._history(symbol, "1d", datetime.now(timezone.utc) - timedelta(days=10),
                               datetime.now(timezone.utc) + timedelta(days=1))
            if df.empty:
                raise BadSymbolError(f"Yahoo Finance: Kein aktueller Kurs für '{symbol}' verfügbar.")
            price = float(df["Close" if "Close" in df.columns else "close"].dropna().iloc[-1])
        return float(price)

    def list_symbols(self) -> list[str]:
        return list(POPULAR_SYMBOLS)

    # ---- account / trading: not supported -------------------------------------------
    def test_connection(self) -> str:
        self.fetch_price("SPY")
        return "Yahoo Finance erreichbar (keine Zugangsdaten nötig, nur Kursdaten)."

    def _no_trading(self):
        raise NotSupportedError(
            "Yahoo Finance ist ein reiner Datenanbieter – Handel und Kontostand werden nicht "
            "unterstützt. Bitte Testmodus (Papierhandel) oder einen Broker wie Alpaca nutzen."
        )

    def fetch_balance(self) -> dict[str, float]:
        self._no_trading()

    def place_order(self, symbol: str, side: Side, quantity: float,
                    order_type: OrderType = OrderType.MARKET,
                    price: float | None = None) -> Order:
        self._no_trading()

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self._no_trading()
