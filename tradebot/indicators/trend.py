"""Trend indicators: moving averages, MACD, ADX, Ichimoku, SAR, Supertrend ..."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import _utils as u
from .registry import Param, indicator

C = "Trend"


def _period(default=20, lo=2, hi=500, label="Periode"):
    return Param("period", default, lo, hi, 1, label, "int")


# --------------------------------------------------------------------------- moving averages
@indicator("sma", "Simple Moving Average", C, [_period(20)], ["sma"], overlay=True,
           description="Einfacher gleitender Durchschnitt der Schlusskurse. "
                       "Glättet den Kurs; Kurs über dem SMA deutet auf Aufwärtstrend hin.")
def sma(df, period=20):
    return u.frame(df, sma=u.sma(df["close"], period))


@indicator("ema", "Exponential Moving Average", C, [_period(20)], ["ema"], overlay=True,
           description="Exponentiell gewichteter Durchschnitt, der neuere Kurse stärker gewichtet. "
                       "Reagiert schneller als der SMA; häufig für Crossover-Signale genutzt.")
def ema(df, period=20):
    return u.frame(df, ema=u.ema(df["close"], period))


@indicator("wma", "Weighted Moving Average", C, [_period(20)], ["wma"], overlay=True,
           description="Linear gewichteter Durchschnitt (neuester Kurs mit höchstem Gewicht). "
                       "Liegt zwischen SMA und EMA in der Reaktionsgeschwindigkeit.")
def wma(df, period=20):
    return u.frame(df, wma=u.wma(df["close"], period))


@indicator("dema", "Double Exponential Moving Average", C, [_period(20)], ["dema"], overlay=True,
           description="Doppelter EMA (2·EMA − EMA(EMA)) mit reduzierter Verzögerung. "
                       "Für schnellere Trendsignale als beim normalen EMA.")
def dema(df, period=20):
    e1 = u.ema(df["close"], period)
    e2 = u.ema(e1, period)
    return u.frame(df, dema=2 * e1 - e2)


@indicator("tema", "Triple Exponential Moving Average", C, [_period(20)], ["tema"], overlay=True,
           description="Dreifacher EMA (3·E1 − 3·E2 + E3) mit sehr geringer Verzögerung. "
                       "Folgt dem Kurs eng und eignet sich für schnelle Trendfilter.")
def tema(df, period=20):
    e1 = u.ema(df["close"], period)
    e2 = u.ema(e1, period)
    e3 = u.ema(e2, period)
    return u.frame(df, tema=3 * e1 - 3 * e2 + e3)


@indicator("hma", "Hull Moving Average", C, [_period(20, 4)], ["hma"], overlay=True,
           description="Hull-MA: WMA(2·WMA(n/2) − WMA(n), √n). "
                       "Sehr glatt und gleichzeitig reaktionsschnell.")
def hma(df, period=20):
    c = df["close"]
    half = max(int(period / 2), 1)
    sq = max(int(np.sqrt(period)), 1)
    raw = 2 * u.wma(c, half) - u.wma(c, period)
    out = pd.Series(np.nan, index=df.index)
    first = raw.first_valid_index()
    if first is not None:
        start = raw.index.get_loc(first)
        out.iloc[start:] = u.wma(raw.iloc[start:], sq).to_numpy()
    return u.frame(df, hma=out)


@indicator("kama", "Kaufman Adaptive Moving Average", C,
           [_period(10), Param("fast", 2, 1, 50, 1, "Schnelle Periode", "int"),
            Param("slow", 30, 2, 200, 1, "Langsame Periode", "int")],
           ["kama"], overlay=True,
           description="Adaptiver Durchschnitt, der sich in Trends beschleunigt und in Seitwärtsphasen "
                       "verlangsamt (Efficiency Ratio). Gut als Trendfilter mit wenig Fehlsignalen.")
def kama(df, period=10, fast=2, slow=30):
    c = df["close"].astype(float)
    n = len(c)
    out = np.full(n, np.nan)
    if n >= period + 1:
        change = (c - c.shift(period)).abs()
        vol = u.rolling_sum(c.diff().abs(), period)
        er = u.safe_div(change, vol).fillna(0.0).to_numpy()
        fsc, ssc = 2.0 / (fast + 1), 2.0 / (slow + 1)
        sc = (er * (fsc - ssc) + ssc) ** 2
        cv = c.to_numpy()
        out[period - 1] = cv[period - 1]
        for i in range(period, n):
            prev = out[i - 1]
            out[i] = prev + sc[i] * (cv[i] - prev) if not np.isnan(cv[i]) else prev
    return u.frame(df, kama=out)


@indicator("vwma", "Volume Weighted Moving Average", C, [_period(20)], ["vwma"], overlay=True,
           description="Volumengewichteter Durchschnitt der Schlusskurse. "
                       "Bewegungen mit hohem Volumen fließen stärker ein.")
def vwma(df, period=20):
    num = u.rolling_sum(df["close"] * df["volume"], period)
    den = u.rolling_sum(df["volume"], period)
    return u.frame(df, vwma=u.safe_div(num, den))


@indicator("zlema", "Zero Lag EMA", C, [_period(20)], ["zlema"], overlay=True,
           description="EMA auf einem um die Verzögerung korrigierten Kurs (2·Close − Close vor (n−1)/2 Bars). "
                       "Reduziert die Trägheit klassischer Durchschnitte.")
def zlema(df, period=20):
    c = df["close"]
    lag = (period - 1) // 2
    src = c + (c - c.shift(lag))
    return u.frame(df, zlema=u.ema(src, period))


# --------------------------------------------------------------------------- MACD / PPO
def _fsp():
    return [Param("fast", 12, 2, 100, 1, "Schnelle Periode", "int"),
            Param("slow", 26, 3, 300, 1, "Langsame Periode", "int"),
            Param("signal", 9, 1, 100, 1, "Signalperiode", "int")]


@indicator("macd", "MACD", C, _fsp(), ["macd", "signal", "hist"], levels=[0],
           description="Differenz aus schnellem und langsamem EMA mit Signallinie. "
                       "Kreuzt die MACD-Linie die Signallinie nach oben, gilt das als Kaufsignal.")
def macd(df, fast=12, slow=26, signal=9):
    c = df["close"]
    m = u.ema(c, fast) - u.ema(c, slow)
    s = u.ema(m, signal)
    return u.frame(df, macd=m, signal=s, hist=m - s)


@indicator("ppo", "Percentage Price Oscillator", C, _fsp(), ["ppo", "signal", "hist"], levels=[0],
           description="Wie MACD, aber in Prozent des langsamen EMA – dadurch zwischen Werten vergleichbar. "
                       "Über 0 liegt der schnelle über dem langsamen Durchschnitt.")
def ppo(df, fast=12, slow=26, signal=9):
    c = df["close"]
    es = u.ema(c, slow)
    p = 100 * u.safe_div(u.ema(c, fast) - es, es)
    s = u.ema(p, signal)
    return u.frame(df, ppo=p, signal=s, hist=p - s)


# --------------------------------------------------------------------------- ADX / Aroon / Vortex
@indicator("adx", "Average Directional Index", C, [_period(14)], ["adx", "plus_di", "minus_di"],
           levels=[20, 25],
           description="Misst die Trendstärke (Wilder) unabhängig von der Richtung; +DI/−DI zeigen die Richtung. "
                       "ADX über 25 gilt als starker Trend, unter 20 als trendlos.")
def adx(df, period=14):
    h, l = df["high"], df["low"]
    tr = u.true_range(df)
    tr.iloc[:1] = np.nan
    up = h.diff()
    down = -l.diff()
    pdm = up.where((up > down) & (up > 0), 0.0).where(up.notna())
    mdm = down.where((down > up) & (down > 0), 0.0).where(down.notna())
    atr = u.rma(tr, period)
    pdi = 100 * u.safe_div(u.rma(pdm, period), atr)
    mdi = 100 * u.safe_div(u.rma(mdm, period), atr)
    dx = 100 * u.safe_div((pdi - mdi).abs(), pdi + mdi)
    return u.frame(df, adx=u.rma(dx, period), plus_di=pdi, minus_di=mdi)


@indicator("aroon", "Aroon", C, [_period(25)], ["up", "down", "osc"], levels=[-50, 50],
           description="Misst, wie viele Bars seit dem höchsten Hoch bzw. tiefsten Tief vergangen sind (0–100). "
                       "Aroon Up über 70 und Down unter 30 deuten auf Aufwärtstrend hin.")
def aroon(df, period=25):
    n = len(df)
    up = np.full(n, np.nan)
    dn = np.full(n, np.nan)
    wh = u.window_view(df["high"], period + 1)
    wl = u.window_view(df["low"], period + 1)
    if wh is not None:
        # bars since most recent extreme (reverse window so ties pick the newest bar)
        since_h = np.argmax(wh[:, ::-1], axis=1)
        since_l = np.argmin(wl[:, ::-1], axis=1)
        valid_h = ~np.isnan(wh).any(axis=1)
        valid_l = ~np.isnan(wl).any(axis=1)
        up[period:] = np.where(valid_h, 100.0 * (period - since_h) / period, np.nan)
        dn[period:] = np.where(valid_l, 100.0 * (period - since_l) / period, np.nan)
    return u.frame(df, up=up, down=dn, osc=up - dn)


@indicator("vortex", "Vortex Indicator", C, [_period(14)], ["plus", "minus"], levels=[1],
           description="Vergleicht Aufwärts- und Abwärtsbewegungen (VI+ / VI−). "
                       "Kreuzt VI+ über VI−, beginnt möglicherweise ein Aufwärtstrend.")
def vortex(df, period=14):
    h, l = df["high"], df["low"]
    vmp = (h - l.shift(1)).abs()
    vmm = (l - h.shift(1)).abs()
    tr = u.true_range(df)
    tr.iloc[:1] = np.nan
    trs = u.rolling_sum(tr, period)
    return u.frame(df, plus=u.safe_div(u.rolling_sum(vmp, period), trs),
                   minus=u.safe_div(u.rolling_sum(vmm, period), trs))


# --------------------------------------------------------------------------- Ichimoku
@indicator("ichimoku", "Ichimoku Kinko Hyo", C,
           [Param("tenkan", 9, 2, 100, 1, "Tenkan-Periode", "int"),
            Param("kijun", 26, 2, 200, 1, "Kijun-Periode", "int"),
            Param("senkou", 52, 2, 300, 1, "Senkou-B-Periode", "int")],
           ["tenkan", "kijun", "span_a", "span_b"], overlay=True,
           description="Tenkan/Kijun sind Mittelwerte aus Hoch und Tief; Span A/B bilden die Wolke. "
                       "Hinweis: Span A/B werden aus aktuellen Daten berechnet und NICHT nach vorne verschoben; "
                       "die Chikou-Linie entfällt (wäre Look-Ahead).")
def ichimoku(df, tenkan=9, kijun=26, senkou=52):
    h, l = df["high"], df["low"]

    def mid(n):
        return (u.highest(h, n) + u.lowest(l, n)) / 2.0

    t, k = mid(tenkan), mid(kijun)
    return u.frame(df, tenkan=t, kijun=k, span_a=(t + k) / 2.0, span_b=mid(senkou))


# --------------------------------------------------------------------------- SAR / Supertrend
@indicator("psar", "Parabolic SAR", C,
           [Param("step", 0.02, 0.001, 0.2, 0.001, "Beschleunigungsfaktor", "float"),
            Param("max_step", 0.2, 0.01, 1.0, 0.01, "Max. Beschleunigung", "float")],
           ["psar"], overlay=True,
           description="Stop-and-Reverse-Punkte nach Wilder, die dem Trend folgen. "
                       "Liegt der SAR unter dem Kurs, herrscht Aufwärtstrend; ein Seitenwechsel signalisiert eine Umkehr.")
def psar(df, step=0.02, max_step=0.2):
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    n = len(df)
    out = np.full(n, np.nan)
    if n < 2:
        return u.frame(df, psar=out)
    up = cl[1] >= cl[0]
    ep = max(hi[0], hi[1]) if up else min(lo[0], lo[1])
    sar = min(lo[0], lo[1]) if up else max(hi[0], hi[1])
    af = step
    out[1] = sar
    for i in range(2, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, lo[i - 1], lo[i - 2])
            if lo[i] < sar:
                up, sar, ep, af = False, ep, lo[i], step
            elif hi[i] > ep:
                ep, af = hi[i], min(af + step, max_step)
        else:
            sar = max(sar, hi[i - 1], hi[i - 2])
            if hi[i] > sar:
                up, sar, ep, af = True, ep, hi[i], step
            elif lo[i] < ep:
                ep, af = lo[i], min(af + step, max_step)
        out[i] = sar
    return u.frame(df, psar=out)


@indicator("supertrend", "Supertrend", C,
           [_period(10, 1, 200, "ATR-Periode"),
            Param("multiplier", 3.0, 0.5, 10.0, 0.1, "ATR-Multiplikator", "float")],
           ["supertrend", "direction"], overlay=True,
           description="ATR-basierte Trendlinie, die bei Trendwechsel die Seite wechselt. "
                       "direction = +1 Aufwärtstrend, −1 Abwärtstrend.")
def supertrend(df, period=10, multiplier=3.0):
    atr = u.atr(df, period).to_numpy()
    mid = u.hl2(df).to_numpy()
    cl = df["close"].to_numpy(dtype=float)
    bu = mid + multiplier * atr
    bl = mid - multiplier * atr
    n = len(df)
    st = np.full(n, np.nan)
    dr = np.full(n, np.nan)
    fu = fl = np.nan
    d = 1
    started = False
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        if not started:
            fu, fl = bu[i], bl[i]
            d = 1 if cl[i] >= mid[i] else -1
            started = True
        else:
            fu = bu[i] if (bu[i] < fu or cl[i - 1] > fu) else fu
            fl = bl[i] if (bl[i] > fl or cl[i - 1] < fl) else fl
            if d == 1 and cl[i] < fl:
                d = -1
            elif d == -1 and cl[i] > fu:
                d = 1
        st[i] = fl if d == 1 else fu
        dr[i] = d
    return u.frame(df, supertrend=st, direction=dr)


# --------------------------------------------------------------------------- TRIX / regression
@indicator("trix", "TRIX", C, [_period(15), Param("signal", 9, 1, 100, 1, "Signalperiode", "int")],
           ["trix", "signal"], levels=[0],
           description="Prozentuale Veränderung eines dreifach geglätteten EMA. "
                       "Nulllinien- und Signallinienkreuzungen dienen als Trendsignale.")
def trix(df, period=15, signal=9):
    e3 = u.ema(u.ema(u.ema(df["close"], period), period), period)
    t = 100 * u.safe_div(e3.diff(), e3.shift(1))
    return u.frame(df, trix=t, signal=u.ema(t, signal))


@indicator("linreg_slope", "Linear Regression Slope", C, [_period(14)], ["slope"], levels=[0],
           description="Steigung der linearen Regressionsgeraden über die letzten n Schlusskurse (Preis pro Bar). "
                       "Positiv = Aufwärtstrend, negativ = Abwärtstrend.")
def linreg_slope(df, period=14):
    out = np.full(len(df), np.nan)
    w = u.window_view(df["close"], period)
    if w is not None and period >= 2:
        x = np.arange(period, dtype=float)
        x -= x.mean()
        out[period - 1:] = w @ x / (x @ x)
    return u.frame(df, slope=out)
