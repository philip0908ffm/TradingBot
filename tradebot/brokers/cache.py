"""Disk cache for historical OHLCV data.

``fetch_history`` keeps one pickle per (broker, symbol, timeframe) under
``cache_dir/<broker>/<symbol>_<tf>.pkl`` together with the time range it covers.
Only the missing edges of a request are fetched from the broker; a request that does not
touch the cached range at all replaces the cache (keeps the file bounded and contiguous).

The most recent, possibly still forming candle is never counted as covered, so it is
re-fetched next time.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tradebot.brokers._utils import empty_ohlcv, normalize_ohlcv, slice_range, timeframe_delta, to_utc
from tradebot.brokers.base import Broker, BrokerError

log = logging.getLogger(__name__)

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def sanitize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", symbol).strip("_") or "symbol"


def cache_key(broker: Broker) -> str:
    """Directory name for a broker; paper brokers use their data broker's key."""
    inner = getattr(broker, "data_broker", None)
    if isinstance(inner, Broker):
        return cache_key(inner)
    key = broker.info().key
    if broker.options.get("sandbox"):
        key += "_sandbox"
    return key


def cache_path(broker: Broker, symbol: str, timeframe: str, cache_dir: Path) -> Path:
    return Path(cache_dir) / cache_key(broker) / f"{sanitize_symbol(symbol)}_{timeframe}.pkl"


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = pd.read_pickle(path)
        if isinstance(data, dict) and "df" in data:
            return data
    except Exception as exc:  # noqa: BLE001 - corrupt cache is not fatal
        log.warning("Ignoring unreadable cache file %s: %s", path, exc)
    return None


def _save(path: Path, df: pd.DataFrame, start: datetime, end: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pd.to_pickle({"df": df, "start": start, "end": end}, tmp)
    os.replace(tmp, path)


def fetch_history(
    broker: Broker,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    cache_dir: Path | None,
) -> pd.DataFrame:
    """Return candles for [start, end] (inclusive), using/updating the disk cache."""
    start, end = to_utc(start), to_utc(end)
    if start is None or end is None:
        raise BrokerError("Für historische Daten werden Start und Ende benötigt.")
    if start > end:
        raise BrokerError("Ungültiger Zeitraum: Start liegt nach dem Ende.")
    if cache_dir is None:
        return slice_range(broker.fetch_ohlcv(symbol, timeframe, start=start, end=end), start, end)

    tf = timeframe_delta(timeframe)
    now = datetime.now(timezone.utc)
    # Everything up to here is complete (closed candles); later candles may still change.
    complete_until = min(end, now - tf)
    path = cache_path(broker, symbol, timeframe, cache_dir)

    with _lock_for(path):
        cached = _load(path)
        if cached is None:
            df = broker.fetch_ohlcv(symbol, timeframe, start=start, end=end)
            df = normalize_ohlcv(df)
            _save(path, df, start, max(start, complete_until))
            return slice_range(df, start, end)

        df: pd.DataFrame = cached["df"]
        c_start: datetime = cached["start"]
        c_end: datetime = cached["end"]

        disjoint = end < c_start - tf or start > c_end + tf
        if disjoint:
            df = normalize_ohlcv(broker.fetch_ohlcv(symbol, timeframe, start=start, end=end))
            _save(path, df, start, max(start, complete_until))
            return slice_range(df, start, end)

        parts = [df]
        new_start, new_end = c_start, c_end
        if start < c_start:
            head = broker.fetch_ohlcv(symbol, timeframe, start=start, end=c_start)
            parts.insert(0, head)
            new_start = start
        if end > c_end:
            tail = broker.fetch_ohlcv(symbol, timeframe, start=c_end, end=end)
            parts.append(tail)
            new_end = max(c_end, complete_until)
        if len(parts) > 1:
            parts = [normalize_ohlcv(p) for p in parts if p is not None and not p.empty]
            merged = pd.concat(parts) if parts else empty_ohlcv()
            # later fetches win (they may contain the updated forming candle)
            df = normalize_ohlcv(merged)
            _save(path, df, new_start, new_end)
        return slice_range(df, start, end)


def clear_cache(cache_dir: Path, broker_key: str | None = None) -> int:
    """Delete cache files (all, or for one broker). Returns number of files removed."""
    root = Path(cache_dir)
    if broker_key:
        root = root / broker_key
    n = 0
    if root.exists():
        for f in root.rglob("*.pkl"):
            f.unlink(missing_ok=True)
            n += 1
    return n
