"""Bot activity (closed trades, open positions) of TEST/LIVE as chart markers and price lines."""
from __future__ import annotations

import bisect

import pandas as pd

from tradebot.core.models import TIMEFRAMES, TradingMode
from tradebot.gui.state import state
from tradebot.gui.theme import fmt_pct

ENTRY, SL, TP, TRAIL = "#3b82f6", "#ea3943", "#16c784", "#f59e0b"
REASONS = {"signal": "Verkauf", "take_profit": "TP", "stop_loss": "SL", "trailing_stop": "Trail",
           "max_bars": "Zeit", "manual": "Manuell", "end_of_data": "Ende"}


def _ts(value) -> int | None:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if ts is pd.NaT:
        return None
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.timestamp())


def _snap(times: list[int], t: int | None, tf_sec: int) -> int | None:
    """Time of the candle that contains ``t`` (None if outside the loaded range)."""
    if t is None or not times or t < times[0] or t > times[-1] + tf_sec:
        return None
    i = bisect.bisect_right(times, t) - 1
    return times[max(i, 0)]


def statuses() -> dict[TradingMode, dict]:
    """Status of both modes (blocking: reads journals from disk when bots are not running)."""
    out = {}
    for mode in (TradingMode.TEST, TradingMode.LIVE):
        try:
            out[mode] = state().manager.status(mode)
        except Exception:  # noqa: BLE001 - no journal yet / unreadable
            out[mode] = {}
    return out


def overlay(symbol: str, broker: str, df: pd.DataFrame, timeframe: str,
            stats: dict[TradingMode, dict]) -> tuple[list[dict], list[dict], dict]:
    """Return (markers, price_lines, summary) for ``symbol`` on the candles in ``df``."""
    markers: list[dict] = []
    lines: list[dict] = []
    summary = {"trades": 0, "positions": 0}
    if df is None or df.empty:
        return markers, lines, summary
    times = [int(x) for x in pd.DatetimeIndex(df.index).as_unit("s").asi8]
    tf = TIMEFRAMES.get(timeframe, 3600)
    for mode, st in stats.items():
        prefix = "LIVE " if mode == TradingMode.LIVE else ""
        tag = "LIVE" if mode == TradingMode.LIVE else "TEST"
        for t in st.get("closed_trades") or []:
            if t.get("symbol") != symbol:
                continue
            summary["trades"] += 1
            et = _snap(times, _ts(t.get("entry_time")), tf)
            if et is not None:
                markers.append({"time": et, "type": "buy", "text": f"{prefix}Kauf"})
            xt = _snap(times, _ts(t.get("exit_time")), tf)
            if xt is not None:
                label = REASONS.get(t.get("exit_reason"), "Verkauf")
                markers.append({"time": xt, "type": "sell",
                                "text": f"{prefix}{label} {fmt_pct(t.get('pnl_pct'), 1)}"})
        for p in st.get("positions") or []:
            if p.get("symbol") != symbol or (p.get("broker") and p.get("broker") != broker):
                continue
            summary["positions"] += 1
            et = _snap(times, _ts(p.get("entry_time")), tf)
            if et is not None:
                markers.append({"time": et, "type": "buy", "text": f"{prefix}Kauf (offen)"})
            lines.append({"price": p.get("entry_price"), "color": ENTRY, "title": f"{tag} Einstieg"})
            lv = p.get("stop_levels") or {}
            if lv.get("stop_loss"):
                lines.append({"price": lv["stop_loss"], "color": SL, "title": f"{tag} SL"})
            if lv.get("trailing_stop"):
                lines.append({"price": lv["trailing_stop"], "color": TRAIL, "title": f"{tag} Trail"})
            if lv.get("take_profit"):
                lines.append({"price": lv["take_profit"], "color": TP, "title": f"{tag} TP"})
    lines = [x for x in lines if x.get("price")]
    return markers, lines, summary
