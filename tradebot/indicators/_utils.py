"""Shared numeric helpers for indicator implementations (pure pandas/numpy).

All helpers are causal: the value at row t only depends on rows <= t.
Recursive smoothers (EMA, Wilder RMA) are seeded with the simple average of the
first ``n`` valid values, so they return NaN during warm-up.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def frame(df: pd.DataFrame, **cols) -> pd.DataFrame:
    """Build an output frame on df's index with float columns (inf -> NaN)."""
    out = pd.DataFrame({k: np.asarray(v, dtype=float) for k, v in cols.items()}, index=df.index)
    return out.replace([np.inf, -np.inf], np.nan)


def safe_div(a, b):
    """Element-wise a / b with division by zero -> NaN."""
    if isinstance(b, pd.Series):
        b = b.where(b != 0)
    else:
        b = np.where(np.asarray(b) != 0, b, np.nan)
    return a / b


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rolling_sum(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).sum()


def rolling_std(s: pd.Series, n: int, ddof: int = 0) -> pd.Series:
    return s.rolling(n, min_periods=n).std(ddof=ddof)


def highest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).max()


def lowest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).min()


def seeded_ewm(s: pd.Series, n: int, alpha: float) -> pd.Series:
    """Exponential smoothing y_t = alpha*x_t + (1-alpha)*y_{t-1}.

    Seeded with the SMA of the first ``n`` values after the first valid value
    (placed at the n-th of them). NaN before. Leading NaNs of ``s`` are skipped.
    """
    s = s.astype(float)
    out = pd.Series(np.nan, index=s.index, dtype=float)
    first = s.first_valid_index()
    if first is None or n < 1:
        return out
    start = s.index.get_loc(first)
    seed_end = start + n  # exclusive
    if seed_end > len(s):
        return out
    seed = s.iloc[start:seed_end].mean()
    tail = s.iloc[seed_end - 1:].copy()
    tail.iloc[0] = seed
    res = tail.ewm(alpha=alpha, adjust=False, ignore_na=True).mean()
    out.iloc[seed_end - 1:] = res.to_numpy()
    return out


def ema(s: pd.Series, n: int) -> pd.Series:
    """Standard EMA (alpha = 2/(n+1)), SMA-seeded."""
    return seeded_ewm(s, n, 2.0 / (n + 1))


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (alpha = 1/n), SMA-seeded."""
    return seeded_ewm(s, n, 1.0 / n)


def window_view(s: pd.Series, n: int) -> np.ndarray | None:
    """Sliding windows (len(s)-n+1, n) of s's values, or None if too short."""
    a = np.asarray(s, dtype=float)
    if n < 1 or len(a) < n:
        return None
    return sliding_window_view(a, n)


def wma(s: pd.Series, n: int) -> pd.Series:
    """Linearly weighted moving average (weights 1..n, newest highest)."""
    out = np.full(len(s), np.nan)
    w = window_view(s, n)
    if w is not None:
        weights = np.arange(1, n + 1, dtype=float)
        out[n - 1:] = w @ weights / weights.sum()
    return pd.Series(out, index=s.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range; the first row uses high - low (no previous close)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1, skipna=True)
    return tr.astype(float)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder's Average True Range."""
    return rma(true_range(df), n)


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3.0


def hl2(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"]) / 2.0
