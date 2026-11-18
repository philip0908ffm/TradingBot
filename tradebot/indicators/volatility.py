"""Volatility indicators: Bollinger, Keltner, Donchian, ATR, NATR, StdDev, HV."""
from __future__ import annotations

import numpy as np

from . import _utils as u
from .registry import Param, indicator

C = "Volatility"


def _period(default=20, lo=2, hi=500, label="Periode"):
    return Param("period", default, lo, hi, 1, label, "int")


@indicator("bbands", "Bollinger Bands", C,
           [_period(20), Param("std", 2.0, 0.5, 5.0, 0.1, "Standardabweichungen", "float")],
           ["upper", "middle", "lower", "percent_b", "bandwidth"], overlay=True,
           description="SMA ± k Standardabweichungen (Grundgesamtheit). percent_b zeigt die Lage im Band (0 = unten, 1 = oben), "
                       "bandwidth die Bandbreite in % des Mittelbands; enge Bänder kündigen oft Ausbrüche an.")
def bbands(df, period=20, std=2.0):
    c = df["close"]
    mid = u.sma(c, period)
    sd = u.rolling_std(c, period, ddof=0)
    up, lo = mid + std * sd, mid - std * sd
    return u.frame(df, upper=up, middle=mid, lower=lo,
                   percent_b=u.safe_div(c - lo, up - lo),
                   bandwidth=100 * u.safe_div(up - lo, mid))


@indicator("keltner", "Keltner Channels", C,
           [_period(20, 2, 500, "EMA-Periode"),
            Param("atr_period", 10, 1, 200, 1, "ATR-Periode", "int"),
            Param("multiplier", 2.0, 0.5, 10.0, 0.1, "ATR-Multiplikator", "float")],
           ["upper", "middle", "lower"], overlay=True,
           description="EMA des Schlusskurses ± Vielfaches der ATR. "
                       "Ausbrüche über/unter den Kanal deuten auf starke Bewegungen hin.")
def keltner(df, period=20, atr_period=10, multiplier=2.0):
    mid = u.ema(df["close"], period)
    a = u.atr(df, atr_period)
    return u.frame(df, upper=mid + multiplier * a, middle=mid, lower=mid - multiplier * a)


@indicator("donchian", "Donchian Channels", C, [_period(20)], ["upper", "middle", "lower"],
           overlay=True,
           description="Höchstes Hoch und tiefstes Tief der letzten n Bars (inkl. aktueller Bar). "
                       "Klassisch für Breakout-Strategien (Turtle Trading).")
def donchian(df, period=20):
    up, lo = u.highest(df["high"], period), u.lowest(df["low"], period)
    return u.frame(df, upper=up, middle=(up + lo) / 2.0, lower=lo)


@indicator("atr", "Average True Range", C, [_period(14, 1)], ["atr"],
           description="Durchschnittliche wahre Handelsspanne nach Wilder, in Preiseinheiten. "
                       "Wird oft für Stop-Loss-Abstände und Positionsgrößen genutzt.")
def atr(df, period=14):
    return u.frame(df, atr=u.atr(df, period))


@indicator("natr", "Normalized ATR", C, [_period(14, 1)], ["natr"],
           description="ATR in Prozent des Schlusskurses. "
                       "Macht die Volatilität zwischen Werten mit unterschiedlichem Preisniveau vergleichbar.")
def natr(df, period=14):
    return u.frame(df, natr=100 * u.safe_div(u.atr(df, period), df["close"]))


@indicator("stddev", "Standard Deviation", C, [_period(20)], ["stddev"],
           description="Rollierende Standardabweichung (Grundgesamtheit) der Schlusskurse in Preiseinheiten. "
                       "Steigende Werte zeigen zunehmende Volatilität.")
def stddev(df, period=20):
    return u.frame(df, stddev=u.rolling_std(df["close"], period, ddof=0))


@indicator("hv", "Historical Volatility", C,
           [_period(20), Param("periods_per_year", 365, 1, 100000, 1, "Perioden pro Jahr", "int")],
           ["hv"],
           description="Annualisierte Standardabweichung der logarithmischen Renditen in %. "
                       "Perioden pro Jahr z. B. 365 (Krypto, täglich) oder 252 (Aktien, täglich).")
def hv(df, period=20, periods_per_year=365):
    r = np.log(df["close"] / df["close"].shift(1))
    return u.frame(df, hv=100 * u.rolling_std(r, period, ddof=1) * np.sqrt(periods_per_year))
