import math

import numpy as np
import pandas as pd
import pytest

from tradebot.indicators import (CATEGORIES, REGISTRY, IndicatorSpec, Param, column_name, compute,
                                 get)


def make_df(n=300, seed=42):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    vol = rng.uniform(100, 1000, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                        index=idx)


DF = make_df()
KEYS = sorted(REGISTRY)


def test_registry_size_and_metadata():
    assert len(REGISTRY) >= 45
    for key, spec in REGISTRY.items():
        assert isinstance(spec, IndicatorSpec)
        assert spec.key == key and spec.category in CATEGORIES
        assert spec.outputs and spec.description
        assert len(set(spec.outputs)) == len(spec.outputs)
        for p in spec.params:
            assert isinstance(p, Param)
            assert p.label and p.type in ("int", "float")
            assert p.min <= p.default <= p.max and p.step > 0
            assert p.name == p.name.lower()


@pytest.mark.parametrize("key", KEYS)
def test_shape_and_columns(key):
    spec = get(key)
    out = compute(key, DF)
    assert list(out.columns) == spec.outputs
    assert out.index.equals(DF.index)
    assert all(out[c].dtype == float for c in out.columns)
    assert out.iloc[-1].notna().all(), f"{key} last row NaN"


@pytest.mark.parametrize("key", KEYS)
def test_short_series(key):
    for n in (0, 1, 5):
        df = DF.iloc[:n]
        out = compute(key, df)
        assert list(out.columns) == get(key).outputs
        assert out.index.equals(df.index)


@pytest.mark.parametrize("key", KEYS)
def test_no_lookahead(key):
    full = compute(key, DF)
    for n in (5, 40, 120, 299):
        part = compute(key, DF.iloc[:n])
        pd.testing.assert_frame_equal(part, full.iloc[:n], check_exact=False, rtol=1e-9, atol=1e-9)


@pytest.mark.parametrize("key", KEYS)
def test_input_not_mutated(key):
    df = DF.copy()
    before = df.copy()
    compute(key, df)
    pd.testing.assert_frame_equal(df, before)
    assert list(df.columns) == list(before.columns)


# ------------------------------------------------------------------ reference implementations
C = DF["close"].to_numpy()
H = DF["high"].to_numpy()
L = DF["low"].to_numpy()
V = DF["volume"].to_numpy()


def ref_sma(x, n):
    return np.array([np.nan if i < n - 1 else np.mean(x[i - n + 1:i + 1]) for i in range(len(x))])


def ref_ema(x, n):
    """SMA-seeded EMA over x (leading NaNs skipped)."""
    out = np.full(len(x), np.nan)
    start = int(np.argmax(~np.isnan(x)))
    if len(x) - start < n:
        return out
    a = 2 / (n + 1)
    out[start + n - 1] = np.mean(x[start:start + n])
    for i in range(start + n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def close_enough(a, b):
    np.testing.assert_allclose(np.asarray(a, float), np.asarray(b, float), rtol=1e-8, atol=1e-8,
                               equal_nan=True)


def test_sma_values():
    close_enough(compute("sma", DF, {"period": 10})["sma"], ref_sma(C, 10))


def test_ema_values():
    close_enough(compute("ema", DF, {"period": 10})["ema"], ref_ema(C, 10))


def test_rsi_wilder_values():
    n = 14
    out = np.full(len(C), np.nan)
    d = np.diff(C)
    g, l_ = np.maximum(d, 0), np.maximum(-d, 0)
    ag, al = g[:n].mean(), l_[:n].mean()
    out[n] = 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(C)):
        ag = (ag * (n - 1) + g[i - 1]) / n
        al = (al * (n - 1) + l_[i - 1]) / n
        out[i] = 100 - 100 / (1 + ag / al)
    close_enough(compute("rsi", DF)["rsi"], out)


def test_rsi_extremes():
    df = DF.copy()
    df["close"] = np.arange(1, len(df) + 1, dtype=float)
    assert compute("rsi", df)["rsi"].iloc[-1] == 100
    df["close"] = 5.0
    assert compute("rsi", df)["rsi"].iloc[-1] == 50


def test_macd_values():
    m = ref_ema(C, 12) - ref_ema(C, 26)
    s = ref_ema(m, 9)
    out = compute("macd", DF)
    close_enough(out["macd"], m)
    close_enough(out["signal"], s)
    close_enough(out["hist"], m - s)
    assert out["signal"].first_valid_index() == DF.index[25 + 8]


def test_bbands_values():
    n, k = 20, 2.0
    mid = ref_sma(C, n)
    sd = np.array([np.nan if i < n - 1 else np.std(C[i - n + 1:i + 1]) for i in range(len(C))])
    out = compute("bbands", DF, {"period": n, "std": k})
    close_enough(out["middle"], mid)
    close_enough(out["upper"], mid + k * sd)
    close_enough(out["lower"], mid - k * sd)
    close_enough(out["percent_b"], (C - (mid - k * sd)) / (2 * k * sd))
    close_enough(out["bandwidth"], 100 * 2 * k * sd / mid)


def test_atr_values():
    n = 14
    tr = np.empty(len(C))
    tr[0] = H[0] - L[0]
    for i in range(1, len(C)):
        tr[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
    ref = np.full(len(C), np.nan)
    ref[n - 1] = tr[:n].mean()
    for i in range(n, len(C)):
        ref[i] = (ref[i - 1] * (n - 1) + tr[i]) / n
    close_enough(compute("atr", DF)["atr"], ref)
    close_enough(compute("natr", DF)["natr"], 100 * ref / C)


def test_stoch_values():
    n, sk, sd = 14, 3, 3
    raw = np.full(len(C), np.nan)
    for i in range(n - 1, len(C)):
        hh, ll = H[i - n + 1:i + 1].max(), L[i - n + 1:i + 1].min()
        raw[i] = 100 * (C[i] - ll) / (hh - ll)
    k = pd.Series(raw).rolling(sk).mean().to_numpy()
    d = pd.Series(k).rolling(sd).mean().to_numpy()
    out = compute("stoch", DF, {"period": n, "smooth_k": sk, "smooth_d": sd})
    close_enough(out["k"], k)
    close_enough(out["d"], d)
    assert ((out["k"].dropna() >= 0) & (out["k"].dropna() <= 100)).all()


def test_obv_values():
    ref = np.zeros(len(C))
    for i in range(1, len(C)):
        ref[i] = ref[i - 1] + (V[i] if C[i] > C[i - 1] else -V[i] if C[i] < C[i - 1] else 0)
    close_enough(compute("obv", DF)["obv"], ref)


def test_misc_values():
    # WMA
    n = 5
    w = np.arange(1, n + 1)
    ref = np.array([np.nan if i < n - 1 else (C[i - n + 1:i + 1] @ w) / w.sum() for i in range(len(C))])
    close_enough(compute("wma", DF, {"period": n})["wma"], ref)
    # Donchian / highest / lowest
    don = compute("donchian", DF, {"period": 10})
    close_enough(don["upper"], compute("highest", DF, {"period": 10})["highest"])
    assert don["upper"].iloc[-1] == H[-10:].max()
    assert don["lower"].iloc[-1] == L[-10:].min()
    # ROC / change
    assert math.isclose(compute("roc", DF, {"period": 10})["roc"].iloc[-1], 100 * (C[-1] / C[-11] - 1))
    # Williams %R bounds
    wr = compute("willr", DF)["willr"].dropna()
    assert ((wr <= 0) & (wr >= -100)).all()
    # Supertrend direction is +1/-1
    assert set(compute("supertrend", DF)["direction"].dropna().unique()) <= {1.0, -1.0}
    # Linear regression slope of a straight line
    df = DF.copy()
    df["close"] = 3.0 * np.arange(len(df)) + 7
    assert np.allclose(compute("linreg_slope", df)["slope"].dropna(), 3.0)
    # Aroon on monotonic rise: up = 100, down = 0
    df["high"] = df["close"] + 1
    df["low"] = df["close"] - 1
    ar = compute("aroon", df, {"period": 10}).dropna()
    assert (ar["up"] == 100).all() and (ar["down"] == 0).all()


def test_compute_params_and_coercion():
    a = compute("rsi", DF, {"period": 7.0, "unknown": 3})
    b = compute("rsi", DF, {"period": 7})
    pd.testing.assert_frame_equal(a, b)
    assert a["rsi"].first_valid_index() == DF.index[7]
    with pytest.raises(KeyError):
        compute("does_not_exist", DF)


def test_column_name():
    assert column_name("rsi", None, "rsi") == "rsi(14).rsi"
    assert column_name("macd", {"fast": 8}, "signal") == "macd(8,26,9).signal"
    assert column_name("bbands", {"std": 2.5}, "upper") == "bbands(20,2.5).upper"
    assert column_name("obv", {}, "obv") == "obv().obv"
