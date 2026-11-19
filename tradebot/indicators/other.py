"""Other / statistical helpers: price change, highest/lowest, z-score, MA distance."""
from __future__ import annotations

from . import _utils as u
from .registry import Param, indicator

C = "Other"


def _period(default=20, lo=1, hi=500, label="Periode"):
    return Param("period", default, lo, hi, 1, label, "int")


@indicator("change", "Price Change %", C, [_period(1)], ["change"], levels=[0],
           description="Prozentuale Veränderung des Schlusskurses gegenüber dem Kurs vor n Bars. "
                       "Nützlich für Momentum- und Ausbruchsfilter.")
def change(df, period=1):
    c = df["close"]
    return u.frame(df, change=100 * u.safe_div(c - c.shift(period), c.shift(period)))


@indicator("highest", "Highest High", C, [_period(20)], ["highest"], overlay=True,
           description="Höchstes Hoch der letzten n Bars (inkl. aktueller Bar). "
                       "Für Ausbruchs- und Trailing-Stop-Regeln.")
def highest(df, period=20):
    return u.frame(df, highest=u.highest(df["high"], period))


@indicator("lowest", "Lowest Low", C, [_period(20)], ["lowest"], overlay=True,
           description="Tiefstes Tief der letzten n Bars (inkl. aktueller Bar). "
                       "Für Ausbruchs- und Stop-Loss-Regeln.")
def lowest(df, period=20):
    return u.frame(df, lowest=u.lowest(df["low"], period))


@indicator("zscore", "Z-Score", C, [_period(20, 2)], ["zscore"], levels=[-2, 0, 2],
           description="Abstand des Schlusskurses vom gleitenden Durchschnitt in Standardabweichungen. "
                       "Werte unter −2 bzw. über 2 gelten als statistisch extrem (Mean Reversion).")
def zscore(df, period=20):
    c = df["close"]
    return u.frame(df, zscore=u.safe_div(c - u.sma(c, period), u.rolling_std(c, period, ddof=0)))


@indicator("ma_dist", "Distance to MA %", C, [_period(50, 1)], ["dist"], levels=[0],
           description="Prozentualer Abstand des Schlusskurses zum einfachen gleitenden Durchschnitt. "
                       "Große Abstände deuten auf überdehnte Bewegungen hin.")
def ma_dist(df, period=50):
    c = df["close"]
    return u.frame(df, dist=100 * u.safe_div(c - u.sma(c, period), u.sma(c, period)))
