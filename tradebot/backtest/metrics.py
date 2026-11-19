"""Performance metrics for backtests.

Conventions
-----------
* ``max_drawdown_pct`` is the POSITIVE magnitude of the largest peak-to-trough decline of
  the equity curve in percent (e.g. 12.5 means -12.5 %).
* CAGR uses calendar time from the index span (crypto trades 24/7, stocks don't – calendar
  time is right for both).
* Sharpe/Sortino/volatility are annualised with bars-per-year estimated from the actual
  data: (number of bars - 1) / index span in years. This automatically handles markets with
  closing hours/weekends. Falls back to the median index spacing and finally to the
  nominal ``TIMEFRAMES`` duration. Risk-free rate = 0.
* Values that are undefined (no trades, zero variance, no losses for profit factor...) are
  ``None``; the output never contains inf/NaN.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tradebot.core.models import TIMEFRAMES, ClosedTrade

SECONDS_PER_YEAR = 365.25 * 86400

# key -> (German label, format) ; formats: "pct", "money", "num", "int"
METRIC_LABELS: dict[str, tuple[str, str]] = {
    "final_equity": ("Endkapital", "money"),
    "total_return_pct": ("Gesamtrendite", "pct"),
    "buy_hold_return_pct": ("Buy & Hold Rendite", "pct"),
    "cagr_pct": ("Rendite p.a. (CAGR)", "pct"),
    "max_drawdown_pct": ("Max. Drawdown (Verlust vom Hoch)", "pct"),
    "max_drawdown_duration_bars": ("Längste Drawdown-Phase (Kerzen)", "int"),
    "volatility_ann_pct": ("Volatilität p.a.", "pct"),
    "sharpe": ("Sharpe Ratio", "num"),
    "sortino": ("Sortino Ratio", "num"),
    "calmar": ("Calmar Ratio", "num"),
    "num_trades": ("Anzahl Trades", "int"),
    "win_rate_pct": ("Trefferquote", "pct"),
    "profit_factor": ("Profit-Faktor", "num"),
    "avg_trade_pct": ("Ø Trade", "pct"),
    "avg_win_pct": ("Ø Gewinn-Trade", "pct"),
    "avg_loss_pct": ("Ø Verlust-Trade", "pct"),
    "best_trade_pct": ("Bester Trade", "pct"),
    "worst_trade_pct": ("Schlechtester Trade", "pct"),
    "avg_bars_held": ("Ø Haltedauer (Kerzen)", "num"),
    "exposure_pct": ("Zeit im Markt", "pct"),
    "total_fees": ("Gebühren gesamt", "money"),
    "expectancy": ("Erwartungswert je Trade", "money"),
}


def _clean(v):
    """float/None without inf/NaN; ints stay ints."""
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _years(index: pd.Index) -> float | None:
    if len(index) < 2 or not isinstance(index, pd.DatetimeIndex):
        return None
    span = (index[-1] - index[0]).total_seconds()
    return span / SECONDS_PER_YEAR if span > 0 else None


def bars_per_year(index: pd.Index, timeframe: str) -> float:
    years = _years(index)
    if years and len(index) > 2:
        return (len(index) - 1) / years
    if isinstance(index, pd.DatetimeIndex) and len(index) >= 2:
        med = pd.Series(index).diff().dt.total_seconds().median()
        if med and med > 0:
            return SECONDS_PER_YEAR / med
    return SECONDS_PER_YEAR / TIMEFRAMES.get(timeframe, 86400)


def drawdown_stats(equity: pd.Series) -> tuple[float, int]:
    """(max drawdown as positive %, longest underwater duration in bars)."""
    eq = equity.to_numpy(dtype=float)
    if len(eq) == 0:
        return 0.0, 0
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
    max_dd = float(np.nanmax(dd)) * 100.0 if len(dd) else 0.0
    longest = cur = 0
    for under in eq < peak:
        cur = cur + 1 if under else 0
        longest = max(longest, cur)
    return max_dd, longest


def _bars_from_times(trades: list[ClosedTrade], index: pd.Index) -> list[int]:
    if not isinstance(index, pd.DatetimeIndex):
        return [1] * len(trades)
    res = []
    for t in trades:
        a = index.searchsorted(pd.Timestamp(t.entry_time))
        b = index.searchsorted(pd.Timestamp(t.exit_time))
        res.append(max(int(b - a), 1))
    return res


def compute_metrics(
    equity: pd.Series,
    trades: list[ClosedTrade],
    buy_hold: pd.Series | None,
    timeframe: str,
    initial_capital: float,
    exposure: pd.Series | None = None,
    bars_held: list[int] | None = None,
) -> dict:
    """Metrics dict (keys = ``METRIC_LABELS``). ``exposure`` (bool per bar, position held)
    and ``bars_held`` (per trade) are optional extras supplied by the backtester; without
    them they are estimated from the trade timestamps."""
    initial_capital = float(initial_capital)
    m: dict = {}
    final = float(equity.iloc[-1]) if len(equity) else initial_capital
    m["final_equity"] = final
    m["total_return_pct"] = (final / initial_capital - 1) * 100 if initial_capital else None
    if buy_hold is not None and len(buy_hold) and initial_capital:
        m["buy_hold_return_pct"] = (float(buy_hold.iloc[-1]) / initial_capital - 1) * 100
    else:
        m["buy_hold_return_pct"] = None

    years = _years(equity.index)
    cagr = None
    if years and initial_capital > 0 and final > 0:
        try:
            cagr = ((final / initial_capital) ** (1 / years) - 1) * 100
        except OverflowError:
            cagr = None
    elif years and final <= 0:
        cagr = -100.0
    m["cagr_pct"] = cagr

    max_dd, dd_dur = drawdown_stats(equity)
    m["max_drawdown_pct"] = max_dd
    m["max_drawdown_duration_bars"] = dd_dur

    bpy = bars_per_year(equity.index, timeframe)
    rets = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna().to_numpy(dtype=float)
    vol = sharpe = sortino = None
    if len(rets) >= 2:
        sd = float(np.std(rets, ddof=1))
        mean = float(np.mean(rets))
        vol = sd * math.sqrt(bpy) * 100
        if sd > 1e-12:
            sharpe = mean / sd * math.sqrt(bpy)
        downside = math.sqrt(float(np.mean(np.minimum(rets, 0.0) ** 2)))
        if downside > 1e-12:
            sortino = mean / downside * math.sqrt(bpy)
    m["volatility_ann_pct"] = vol
    m["sharpe"] = sharpe
    m["sortino"] = sortino
    m["calmar"] = cagr / max_dd if cagr is not None and max_dd > 1e-12 else None

    n = len(trades)
    m["num_trades"] = n
    pcts = np.array([t.pnl_pct for t in trades], dtype=float)
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pcts[pnls > 0], pcts[pnls <= 0]
    gross_win = float(pnls[pnls > 0].sum())
    gross_loss = float(-pnls[pnls < 0].sum())
    m["win_rate_pct"] = len(wins) / n * 100 if n else None
    m["profit_factor"] = gross_win / gross_loss if gross_loss > 0 else None
    m["avg_trade_pct"] = float(pcts.mean()) if n else None
    m["avg_win_pct"] = float(wins.mean()) if len(wins) else None
    m["avg_loss_pct"] = float(losses.mean()) if len(losses) else None
    m["best_trade_pct"] = float(pcts.max()) if n else None
    m["worst_trade_pct"] = float(pcts.min()) if n else None
    if bars_held is None or len(bars_held) != n:
        bars_held = _bars_from_times(trades, equity.index)
    m["avg_bars_held"] = float(np.mean(bars_held)) if n else None

    if exposure is not None and len(exposure):
        m["exposure_pct"] = float(np.asarray(exposure, dtype=bool).mean() * 100)
    elif len(equity):
        m["exposure_pct"] = min(sum(bars_held) / len(equity) * 100, 100.0) if n else 0.0
    else:
        m["exposure_pct"] = 0.0
    m["total_fees"] = float(sum(t.fees for t in trades))
    m["expectancy"] = float(pnls.mean()) if n else None

    return {k: _clean(m.get(k)) for k in METRIC_LABELS}
