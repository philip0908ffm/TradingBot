"""Volume indicators: OBV, CMF, A/D, VWAP, volume ratio, Force Index, Ease of Movement."""
from __future__ import annotations

import numpy as np

from . import _utils as u
from .registry import Param, indicator

C = "Volume"


def _period(default=20, lo=1, hi=500, label="Periode"):
    return Param("period", default, lo, hi, 1, label, "int")


def _mfm(df):
    """Money-flow multiplier ((C-L) - (H-C)) / (H-L); 0 when H == L."""
    h, l, c = df["high"], df["low"], df["close"]
    return u.safe_div((c - l) - (h - c), h - l).fillna(0.0).where(c.notna())


@indicator("obv", "On-Balance Volume", C, [], ["obv"],
           description="Kumuliertes Volumen: an Aufwärtstagen addiert, an Abwärtstagen subtrahiert (Start bei 0). "
                       "Divergenzen zwischen OBV und Kurs deuten auf Trendwechsel hin.")
def obv(df):
    sign = np.sign(df["close"].diff()).fillna(0.0)
    return u.frame(df, obv=(sign * df["volume"]).cumsum())


@indicator("cmf", "Chaikin Money Flow", C, [_period(20, 2)], ["cmf"], levels=[0],
           description="Summe des volumengewichteten Geldflusses relativ zum Volumen über n Bars (−1 bis 1). "
                       "Über 0 überwiegt Kaufdruck, unter 0 Verkaufsdruck.")
def cmf(df, period=20):
    v = df["volume"]
    return u.frame(df, cmf=u.safe_div(u.rolling_sum(_mfm(df) * v, period), u.rolling_sum(v, period)))


@indicator("ad", "Accumulation/Distribution", C, [], ["ad"],
           description="Kumulierte Linie aus Geldfluss-Multiplikator × Volumen (Chaikin). "
                       "Steigende Linie zeigt Akkumulation, fallende Distribution.")
def ad(df):
    return u.frame(df, ad=(_mfm(df) * df["volume"]).cumsum())


@indicator("vwap", "Rolling VWAP", C, [_period(20)], ["vwap"], overlay=True,
           description="Volumengewichteter Durchschnittspreis (typischer Preis) über die letzten n Bars. "
                       "Kurs über dem VWAP gilt als bullisch, darunter als bärisch.")
def vwap(df, period=20):
    v = df["volume"]
    return u.frame(df, vwap=u.safe_div(u.rolling_sum(u.typical_price(df) * v, period),
                                       u.rolling_sum(v, period)))


@indicator("volume_ratio", "Volume Ratio", C, [_period(20)], ["ratio"], levels=[1],
           description="Aktuelles Volumen geteilt durch den Volumen-Durchschnitt der letzten n Bars. "
                       "Werte über 2 zeigen ungewöhnlich hohes Volumen an.")
def volume_ratio(df, period=20):
    v = df["volume"]
    return u.frame(df, ratio=u.safe_div(v, u.sma(v, period)))


@indicator("efi", "Force Index", C, [_period(13)], ["efi"], levels=[0],
           description="EMA von Kursänderung × Volumen (Elder). "
                       "Über 0 dominieren Käufer, unter 0 Verkäufer.")
def efi(df, period=13):
    return u.frame(df, efi=u.ema(df["close"].diff() * df["volume"], period))


@indicator("eom", "Ease of Movement", C,
           [_period(14), Param("divisor", 100000000.0, 1.0, 1e12, 1.0, "Volumen-Divisor", "float")],
           ["eom"], levels=[0],
           description="Setzt Kursbewegung ins Verhältnis zum Volumen (SMA geglättet). "
                       "Positive Werte: Kurs steigt mit wenig Volumen leicht an; negative: leichter Abwärtsdruck.")
def eom(df, period=14, divisor=100000000.0):
    mid = u.hl2(df)
    dist = mid.diff()
    box = u.safe_div(df["volume"] / divisor, df["high"] - df["low"])
    emv = u.safe_div(dist, box).replace([np.inf, -np.inf], np.nan).fillna(0.0).where(dist.notna())
    return u.frame(df, eom=u.sma(emv, period))
