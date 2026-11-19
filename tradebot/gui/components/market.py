"""Market-data helpers for the GUI (blocking – always call through ``run.io_bound``).

* ``broker_for(key)``      cached broker instance with the stored credentials/options
* ``load_candles(...)``    the most recent ``n`` candles (uses the disk cache)
* ``snapshot(item)``       last price, 24h change and a sparkline series for the dashboard
* ``friendly_error(exc)``  short German error text for the UI
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import pandas as pd

from tradebot.brokers import BROKERS, BrokerError, create_broker, fetch_history
from tradebot.brokers.base import Broker
from tradebot.core.models import TIMEFRAMES, AssetClass, WatchItem
from tradebot.gui.state import state

log = logging.getLogger(__name__)

_brokers: dict[str, tuple[tuple, Broker]] = {}
_lock = threading.Lock()


def broker_names() -> dict[str, str]:
    """{key: display name} of all registered brokers."""
    return {k: cls.info().name for k, cls in BROKERS.items()}


def broker_for(key: str) -> Broker:
    """Broker instance (re-created when credentials/options change)."""
    st = state()
    creds = st.store.get_credentials(key) or None
    opts = dict(st.settings.broker_options.get(key) or {})
    sig = (tuple(sorted((creds or {}).items())), tuple(sorted(opts.items())))
    with _lock:
        cached = _brokers.get(key)
        if cached and cached[0] == sig:
            return cached[1]
        broker = create_broker(key, creds, opts)
        _brokers[key] = (sig, broker)
        return broker


def invalidate_broker(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _brokers.clear()
        else:
            _brokers.pop(key, None)


def is_crypto(symbol: str, broker: str, asset_class: AssetClass | str | None = None) -> bool:
    if asset_class is not None:
        return AssetClass(asset_class) == AssetClass.CRYPTO
    if broker in ("binance", "kraken") or "/" in symbol:
        return True
    return symbol.upper().endswith(("-USD", "-EUR", "-USDT"))


def load_candles(broker_key: str, symbol: str, timeframe: str, n: int, crypto: bool = True) -> pd.DataFrame:
    """The most recent ``n`` candles of ``symbol`` (blocking)."""
    tf = TIMEFRAMES[timeframe]
    # stock markets are closed nights/weekends -> request a longer calendar span
    factor = 1.0 if crypto else (1.6 if tf >= 86400 else 5.0)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=tf * n * factor + (0 if crypto else 4 * 86400))
    broker = broker_for(broker_key)
    df = fetch_history(broker, symbol, timeframe, start, end, state().store.cache_dir)
    return df.tail(n)


def snapshot(item: WatchItem) -> dict:
    """Last price, 24h change % and the last ~48 hourly closes (blocking)."""
    crypto = is_crypto(item.symbol, item.broker, item.asset_class)
    df = load_candles(item.broker, item.symbol, "1h", 48 if crypto else 60, crypto)
    if df.empty:
        raise BrokerError("Keine Kursdaten erhalten")
    last = float(df["close"].iloc[-1])
    last_time = df.index[-1]
    ref = df[df.index <= last_time - pd.Timedelta(hours=24)]
    prev = float(ref["close"].iloc[-1]) if not ref.empty else float(df["open"].iloc[0])
    change = (last / prev - 1.0) * 100.0 if prev else None
    spark = df["close"].tail(48)
    return {"price": last, "change_pct": change, "spark": [float(x) for x in spark.to_numpy()],
            "time": last_time.to_pydatetime(), "high": float(spark.max()), "low": float(spark.min())}


def friendly_error(exc: BaseException) -> str:
    """Short, user friendly German error description."""
    msg = str(exc).strip() or type(exc).__name__
    low = msg.lower()
    if "451" in low or "restricted location" in low:
        return ("Binance ist in dieser Region gesperrt – in den Einstellungen den "
                "öffentlichen Daten-Mirror aktivieren.")
    if any(s in low for s in ("timed out", "timeout", "connection", "network", "name resolution",
                              "getaddrinfo", "unreachable")):
        return "Keine Verbindung zum Datenanbieter – Internetverbindung prüfen."
    if len(msg) > 180:
        msg = msg[:177] + "…"
    return msg
