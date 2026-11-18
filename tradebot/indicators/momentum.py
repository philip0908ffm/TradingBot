"""Momentum indicators / oscillators: RSI, Stochastic, CCI, ROC, TSI, MFI ..."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import _utils as u
from .registry import Param, indicator

C = "Momentum"


def _period(default=14, lo=2, hi=500, label="Periode"):
    return Param("period", default, lo, hi, 1, label, "int")


def _rsi(close: pd.Series, period: int) -> pd.Series:
    d = close.diff()
    gain = u.rma(d.clip(lower=0), period)
    loss = u.rma((-d).clip(lower=0), period)
    rsi = 100 - 100 / (1 + u.safe_div(gain, loss))
    rsi = rsi.where(loss != 0, 100.0)  # only gains -> 100
    rsi = rsi.where(~((gain == 0) & (loss == 0)), 50.0)  # flat -> 50
    return rsi.where(gain.notna() & loss.notna())


@indicator("rsi", "Relative Strength Index", C, [_period(14)], ["rsi"], levels=[30, 70],
           description="Verhältnis durchschnittlicher Gewinne zu Verlusten (Wilder-Glättung), Skala 0–100. "
                       "Unter 30 gilt als überverkauft, über 70 als überkauft.")
def rsi(df, period=14):
    return u.frame(df, rsi=_rsi(df["close"], period))


@indicator("stochrsi", "Stochastic RSI", C,
           [Param("rsi_period", 14, 2, 200, 1, "RSI-Periode", "int"),
            _period(14, 2, 200, "Stochastik-Periode"),
            Param("smooth_k", 3, 1, 50, 1, "Glättung %K", "int"),
            Param("smooth_d", 3, 1, 50, 1, "Glättung %D", "int")],
           ["k", "d"], levels=[20, 80],
           description="Stochastik-Oszillator angewendet auf den RSI (0–100), sehr empfindlich. "
                       "Unter 20 gilt als überverkauft, über 80 als überkauft.")
def stochrsi(df, rsi_period=14, period=14, smooth_k=3, smooth_d=3):
    r = _rsi(df["close"], rsi_period)
    lo, hi = u.lowest(r, period), u.highest(r, period)
    raw = 100 * u.safe_div(r - lo, hi - lo)
    k = u.sma(raw, smooth_k)
    return u.frame(df, k=k, d=u.sma(k, smooth_d))


@indicator("stoch", "Stochastic Oscillator", C,
           [_period(14, 2, 200, "%K-Periode"),
            Param("smooth_k", 3, 1, 50, 1, "Glättung %K", "int"),
            Param("smooth_d", 3, 1, 50, 1, "Glättung %D", "int")],
           ["k", "d"], levels=[20, 80],
           description="Lage des Schlusskurses innerhalb der Hoch-Tief-Spanne der letzten n Bars (0–100). "
                       "Unter 20 gilt als überverkauft, über 80 als überkauft.")
def stoch(df, period=14, smooth_k=3, smooth_d=3):
    lo, hi = u.lowest(df["low"], period), u.highest(df["high"], period)
    raw = 100 * u.safe_div(df["close"] - lo, hi - lo)
    k = u.sma(raw, smooth_k)
    return u.frame(df, k=k, d=u.sma(k, smooth_d))


@indicator("willr", "Williams %R", C, [_period(14)], ["willr"], levels=[-80, -20],
           description="Abstand des Schlusskurses zum Hoch der letzten n Bars (−100 bis 0). "
                       "Unter −80 gilt als überverkauft, über −20 als überkauft.")
def willr(df, period=14):
    lo, hi = u.lowest(df["low"], period), u.highest(df["high"], period)
    return u.frame(df, willr=-100 * u.safe_div(hi - df["close"], hi - lo))


@indicator("cci", "Commodity Channel Index", C, [_period(20)], ["cci"], levels=[-100, 100],
           description="Abweichung des typischen Preises von seinem Durchschnitt relativ zur mittleren Abweichung. "
                       "Über +100 gilt als überkauft/starker Trend, unter −100 als überverkauft.")
def cci(df, period=20):
    tp = u.typical_price(df)
    out = np.full(len(df), np.nan)
    w = u.window_view(tp, period)
    if w is not None:
        mean = w.mean(axis=1)
        md = np.abs(w - mean[:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[period - 1:] = (w[:, -1] - mean) / (0.015 * md)
    return u.frame(df, cci=out)


@indicator("roc", "Rate of Change", C, [_period(10, 1)], ["roc"], levels=[0],
           description="Prozentuale Kursveränderung gegenüber dem Kurs vor n Bars. "
                       "Über 0 steigendes, unter 0 fallendes Momentum.")
def roc(df, period=10):
    c = df["close"]
    return u.frame(df, roc=100 * u.safe_div(c - c.shift(period), c.shift(period)))


@indicator("mom", "Momentum", C, [_period(10, 1)], ["mom"], levels=[0],
           description="Absolute Kursdifferenz zum Kurs vor n Bars. "
                       "Nulllinienkreuzungen zeigen Wechsel der Kursdynamik.")
def mom(df, period=10):
    c = df["close"]
    return u.frame(df, mom=c - c.shift(period))


@indicator("ao", "Awesome Oscillator", C,
           [Param("fast", 5, 1, 100, 1, "Schnelle Periode", "int"),
            Param("slow", 34, 2, 300, 1, "Langsame Periode", "int")],
           ["ao"], levels=[0],
           description="Differenz zweier SMAs des Medianpreises (5 und 34). "
                       "Nulllinienkreuzungen und 'Saucer'-Muster dienen als Signale.")
def ao(df, fast=5, slow=34):
    m = u.hl2(df)
    return u.frame(df, ao=u.sma(m, fast) - u.sma(m, slow))


@indicator("uo", "Ultimate Oscillator", C,
           [Param("fast", 7, 1, 100, 1, "Kurze Periode", "int"),
            Param("medium", 14, 2, 200, 1, "Mittlere Periode", "int"),
            Param("slow", 28, 3, 300, 1, "Lange Periode", "int")],
           ["uo"], levels=[30, 70],
           description="Gewichtete Kombination des Kaufdrucks über drei Zeiträume (0–100). "
                       "Unter 30 gilt als überverkauft, über 70 als überkauft.")
def uo(df, fast=7, medium=14, slow=28):
    pc = df["close"].shift(1)
    low_ = pd.concat([df["low"], pc], axis=1).min(axis=1)
    high_ = pd.concat([df["high"], pc], axis=1).max(axis=1)
    bp = (df["close"] - low_).where(pc.notna())
    tr = (high_ - low_).where(pc.notna())

    def avg(n):
        return u.safe_div(u.rolling_sum(bp, n), u.rolling_sum(tr, n))

    return u.frame(df, uo=100 * (4 * avg(fast) + 2 * avg(medium) + avg(slow)) / 7.0)


@indicator("tsi", "True Strength Index", C,
           [Param("slow", 25, 2, 200, 1, "Lange Glättung", "int"),
            Param("fast", 13, 1, 100, 1, "Kurze Glättung", "int"),
            Param("signal", 13, 1, 100, 1, "Signalperiode", "int")],
           ["tsi", "signal"], levels=[-25, 0, 25],
           description="Doppelt geglättetes Momentum der Kursänderungen (−100 bis 100). "
                       "Kreuzungen mit der Signallinie und der Nulllinie liefern Signale.")
def tsi(df, slow=25, fast=13, signal=13):
    d = df["close"].diff()
    num = u.ema(u.ema(d, slow), fast)
    den = u.ema(u.ema(d.abs(), slow), fast)
    t = 100 * u.safe_div(num, den)
    return u.frame(df, tsi=t, signal=u.ema(t, signal))


@indicator("cmo", "Chande Momentum Oscillator", C, [_period(14)], ["cmo"], levels=[-50, 50],
           description="Verhältnis der Summe der Gewinne minus Verluste zu deren Gesamtsumme (−100 bis 100). "
                       "Über +50 gilt als überkauft, unter −50 als überverkauft.")
def cmo(df, period=14):
    d = df["close"].diff()
    up = u.rolling_sum(d.clip(lower=0), period)
    dn = u.rolling_sum((-d).clip(lower=0), period)
    return u.frame(df, cmo=100 * u.safe_div(up - dn, up + dn))


@indicator("mfi", "Money Flow Index", C, [_period(14)], ["mfi"], levels=[20, 80],
           description="Volumengewichteter RSI auf Basis des typischen Preises (0–100). "
                       "Unter 20 gilt als überverkauft, über 80 als überkauft.")
def mfi(df, period=14):
    tp = u.typical_price(df)
    mf = tp * df["volume"]
    dtp = tp.diff()
    pos = u.rolling_sum(mf.where(dtp > 0, 0.0).where(dtp.notna()), period)
    neg = u.rolling_sum(mf.where(dtp < 0, 0.0).where(dtp.notna()), period)
    m = 100 * u.safe_div(pos, pos + neg)
    return u.frame(df, mfi=m)
