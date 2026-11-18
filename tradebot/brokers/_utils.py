"""Small helpers shared by the broker implementations (internal)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from tradebot.brokers.base import BrokerError
from tradebot.core.models import OHLCV_COLUMNS, TIMEFRAMES


def to_utc(dt: datetime | pd.Timestamp | None) -> datetime | None:
    """Return a timezone-aware UTC datetime (naive input is interpreted as UTC)."""
    if dt is None:
        return None
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAMES[timeframe]
    except KeyError:
        raise BrokerError(
            f"Nicht unterstützter Zeitrahmen '{timeframe}'. "
            f"Erlaubt: {', '.join(TIMEFRAMES)}"
        ) from None


def timeframe_delta(timeframe: str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(timeframe))


def empty_ohlcv() -> pd.DataFrame:
    df = pd.DataFrame(columns=OHLCV_COLUMNS, dtype=float)
    df.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return df


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case columns, float dtype, UTC index, sorted, de-duplicated (last wins)."""
    if df is None or df.empty:
        return empty_ohlcv()
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise BrokerError(f"Unvollständige Kursdaten (fehlende Spalten: {', '.join(missing)})")
    df = df[OHLCV_COLUMNS].astype(float)
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = idx.rename("timestamp")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def ohlcv_from_rows(rows: list[list]) -> pd.DataFrame:
    """Build a normalized frame from ccxt-style rows ``[ms, o, h, l, c, v]``."""
    if not rows:
        return empty_ohlcv()
    df = pd.DataFrame([r[:6] for r in rows], columns=["timestamp", *OHLCV_COLUMNS])
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("timestamp")
    df["volume"] = df["volume"].fillna(0.0)
    return normalize_ohlcv(df)


def slice_range(df: pd.DataFrame, start: datetime | None, end: datetime | None) -> pd.DataFrame:
    if df.empty:
        return df
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df
