"""TradingView Lightweight Charts component + helpers to build payloads from pandas data.

Usage::

    chart = TVChart().classes('w-full h-[560px]')
    chart.show(df=ohlcv, indicators=[('ema', {'period': 50}), ('rsi', {'period': 14})],
               markers=result.markers)

Payload format (``TVChart.set_payload``):
    candles      [{time, open, high, low, close}]   time = unix seconds (UTC)
    volume       [{time, value, color}]
    lines        [{title, color, data:[{time,value}], area?:bool, style?:'solid'|'dashed'|'dotted'}]
    overlays     [{title, color, data, width?, style?}]      lines on the price pane
    panes        [{title, height, levels:[..], series:[{title,color,data,type:'line'|'histogram'}]}]
    markers      [{time, position:'aboveBar'|'belowBar', color, shape:'arrowUp'|'arrowDown'|'circle', text}]
    price_lines  [{price, color, title}]
    precision    int (price decimals); fit bool; keep_range bool
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
from nicegui import ui

from tradebot.indicators import compute, get

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "lwc"

PALETTE = ["#3b82f6", "#f59e0b", "#a855f7", "#14b8a6", "#ec4899", "#84cc16", "#f97316", "#06b6d4"]
UP, DOWN = "#16c784", "#ea3943"


class TVChart(ui.element, component="tv_chart.js", esm={"tb-lwc": str(VENDOR)}):
    """Candlestick/line chart with overlay indicators, oscillator panes and trade markers."""

    def __init__(self, theme: str = "dark") -> None:
        super().__init__()
        self._props["theme"] = theme
        self._props["payload"] = None

    def set_theme(self, theme: str) -> None:
        self._props["theme"] = theme
        self.update()

    def set_payload(self, payload: dict) -> None:
        payload = _clean(payload)
        self._props["payload"] = payload  # used on (re)mount
        self.run_method("setData", payload)

    def show(self, df: pd.DataFrame | None = None, indicators: list[tuple[str, dict]] | None = None,
             markers: list[dict] | None = None, price_lines: list[dict] | None = None,
             lines: list[dict] | None = None, volume: bool = True, keep_range: bool = False) -> None:
        """Convenience: build the payload from OHLCV + indicator specs and display it."""
        self.set_payload(build_payload(df, indicators, markers, price_lines, lines, volume, keep_range))


# ---------------------------------------------------------------------------------------
# payload builders
# ---------------------------------------------------------------------------------------

def _ts(index: pd.Index) -> list[int]:
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.as_unit("s").asi8.astype(int).tolist()


def series_points(s: pd.Series) -> list[dict]:
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    return [{"time": t, "value": float(v)} for t, v in zip(_ts(s.index), s.to_numpy())]


def candles_payload(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    ts = _ts(df.index)
    o, h, lo, c, v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    candles = [{"time": t, "open": o[i], "high": h[i], "low": lo[i], "close": c[i]} for i, t in enumerate(ts)]
    vol = [{"time": t, "value": v[i], "color": (UP if c[i] >= o[i] else DOWN) + "66"} for i, t in enumerate(ts)]
    return candles, vol


def price_precision(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 2
    p = float(df["close"].iloc[-1])
    if p <= 0:
        return 2
    return int(min(8, max(2, 4 - math.floor(math.log10(p)))))


def indicator_payload(df: pd.DataFrame, key: str, params: dict, color_offset: int = 0):
    """Return ('overlay', [overlays]) or ('pane', pane_dict) for one indicator."""
    spec = get(key)
    out = compute(key, df, params)
    args = ",".join(f"{v:g}" for v in {**spec.default_params(), **(params or {})}.values()) if spec.params else ""
    label = f"{key.upper()}({args})" if args else key.upper()
    items = []
    ref = float(df["close"].median()) if len(df) else 0.0
    for i, col in enumerate(spec.outputs):
        if spec.overlay and not _on_price_scale(out[col], ref):
            continue  # e.g. Bollinger %B / bandwidth, Supertrend direction
        color = PALETTE[(color_offset + i) % len(PALETTE)]
        title = label if len(spec.outputs) == 1 else f"{label} {col}"
        typ = "histogram" if col in ("hist",) else "line"
        items.append({"title": title, "color": color, "data": series_points(out[col]), "type": typ})
    if spec.overlay:
        return "overlay", items
    return "pane", {"title": label, "height": 130, "levels": list(spec.levels), "series": items}


def _on_price_scale(s: pd.Series, ref: float) -> bool:
    m = s.replace([np.inf, -np.inf], np.nan).dropna()
    if m.empty or ref <= 0:
        return True
    med = float(m.abs().median())
    return 0.5 * ref <= med <= 2.0 * ref


def build_payload(df, indicators=None, markers=None, price_lines=None, lines=None, volume=True,
                  keep_range=False) -> dict:
    payload: dict = {"overlays": [], "panes": [], "markers": [], "price_lines": price_lines or [],
                     "lines": lines or [], "keep_range": keep_range}
    if df is not None and not df.empty:
        candles, vol = candles_payload(df)
        payload["candles"] = candles
        if volume:
            payload["volume"] = vol
        payload["precision"] = price_precision(df)
        offset = 0
        for key, params in indicators or []:
            kind, item = indicator_payload(df, key, params, offset)
            offset += len(get(key).outputs)
            if kind == "overlay":
                payload["overlays"].extend(item)
            else:
                payload["panes"].append(item)
    for m in markers or []:
        buy = m.get("type") == "buy"
        payload["markers"].append({
            "time": int(m["time"]), "position": "belowBar" if buy else "aboveBar",
            "color": UP if buy else DOWN, "shape": "arrowUp" if buy else "arrowDown",
            "text": m.get("text") or ("Kauf" if buy else _reason_label(m.get("reason"))),
        })
    return payload


REASON_LABELS = {"signal": "Verkauf", "take_profit": "TP", "stop_loss": "SL", "trailing_stop": "Trail",
                 "max_bars": "Zeit", "manual": "Manuell", "end_of_data": "Ende"}


def _reason_label(reason: str | None) -> str:
    return REASON_LABELS.get(reason or "signal", "Verkauf")


def _clean(o):
    """Replace NaN/inf (not JSON serialisable) recursively."""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating,)):
        return _clean(float(o))
    if isinstance(o, (np.integer,)):
        return int(o)
    return o
